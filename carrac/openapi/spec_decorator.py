"""
`@spec(...)` decorator + registry walker for auto-generating OpenAPI 3.x.

Intent:
  - Eliminate drift between hand-written path files and real Flask routes.
  - Let authors document an endpoint in one place (next to the handler).
  - Co-exist with hand-written carrac/openapi/paths/*.py during migration;
    auto-entries take precedence when both describe the same operation.

Usage:
    @bp.route('/api/plays', methods=['POST'])
    @auth_or_key_required
    @spec(
        summary='Create a play',
        tags=['Plays'],
        request_body='Play',
        responses={201: 'Play', 400: 'Error'},
        security=[{'apiKey': []}],
        min_tier='premium',
    )
    def play_create():
        ...

The decorator stores metadata on the wrapped function as `_openapi_spec`;
build_auto_paths() then walks app.url_map to collect them.
"""

import re

# Flask converter → OpenAPI path param:  /a/<int:x>/b  →  /a/{x}/b
_PARAM_RE = re.compile(r'<(?:([^:>]+):)?([^>]+)>')

# Converter → OpenAPI schema type
_CONVERTER_TYPES = {
    'int':    {'type': 'integer'},
    'float':  {'type': 'number'},
    'string': {'type': 'string'},
    'path':   {'type': 'string'},
    'uuid':   {'type': 'string', 'format': 'uuid'},
    None:     {'type': 'string'},
}


def spec(
    *,
    summary=None,
    description=None,
    tags=None,
    request_body=None,
    responses=None,
    parameters=None,
    security=None,
    min_tier=None,
    deprecated=False,
    operation_id=None,
    internal=False,
):
    """Attach OpenAPI metadata to a Flask view function.

    Args:
        summary:       one-line operation summary (string).
        description:   longer description (markdown allowed).
        tags:          list of tag strings (e.g. ['Plays']).
        request_body:  either a schema component name (str), a raw schema dict,
                       or None for no body. String → $ref: #/components/schemas/<name>.
        responses:     dict {status_code: schema_name_or_dict_or_description}.
                       Example: {200: 'Play', 404: 'Error'}.
        parameters:    list of extra parameter dicts (query/header). Path params
                       are inferred automatically from the Flask rule.
        security:      list of security requirement dicts, or None to inherit
                       global default. Example: [{'apiKey': []}].
        min_tier:      'free'|'member'|'tester'|'premium'|'admin' — exposed as
                       x-min-tier extension for clients. Documentation only:
                       the decorator never enforces it.
        deprecated:    mark operation as deprecated in the spec.
        operation_id:  override the operationId (defaults to function name).
        internal:      if True, do NOT emit this operation in the public spec.
                       Used for admin/ops routes we don't want to publish.
    """
    def decorator(fn):
        fn._openapi_spec = {
            'summary': summary,
            'description': description,
            'tags': tags or [],
            'request_body': request_body,
            'responses': responses or {200: {'description': 'OK'}},
            'parameters': parameters or [],
            'security': security,
            'min_tier': min_tier,
            'deprecated': deprecated,
            'operation_id': operation_id,
            'internal': internal,
        }
        return fn
    return decorator


# ── Registry walker ────────────────────────────────────────────────

def _flask_rule_to_openapi(rule):
    """Convert a Flask URL rule into (openapi_path, [params])."""
    params = []
    def _sub(m):
        converter, name = m.group(1), m.group(2)
        params.append({
            'name': name,
            'in': 'path',
            'required': True,
            'schema': dict(_CONVERTER_TYPES.get(converter, _CONVERTER_TYPES[None])),
        })
        return '{' + name + '}'
    path = _PARAM_RE.sub(_sub, rule)
    return path, params


def _resolve_request_body(body):
    """Turn the request_body value into a full requestBody OpenAPI object."""
    if body is None:
        return None
    if isinstance(body, str):
        schema = {'$ref': f'#/components/schemas/{body}'}
    else:
        schema = body
    return {
        'required': True,
        'content': {'application/json': {'schema': schema}},
    }


def _resolve_responses(responses):
    """Turn {code: schema_name_or_dict_or_desc} into full OpenAPI responses dict."""
    out = {}
    for code, value in responses.items():
        code_str = str(code)
        if isinstance(value, dict) and ('description' in value or 'content' in value):
            # Caller passed a full response object.
            out[code_str] = value
            continue
        if isinstance(value, str) and value.endswith(':desc'):
            # plain description
            out[code_str] = {'description': value[:-5]}
            continue
        if isinstance(value, str):
            # schema component name
            out[code_str] = {
                'description': 'OK' if code_str.startswith('2') else value,
                'content': {'application/json': {
                    'schema': {'$ref': f'#/components/schemas/{value}'},
                }},
            }
            continue
        # raw schema dict
        out[code_str] = {
            'description': 'OK' if code_str.startswith('2') else '',
            'content': {'application/json': {'schema': value}},
        }
    return out


def build_auto_paths(app, include_internal=False):
    """Walk app.url_map; return OpenAPI `paths` dict for @spec-decorated views.

    Args:
        app: Flask application.
        include_internal: if True, include operations flagged `internal=True`.

    Returns:
        {path: {method: operation, ...}, ...}
    """
    paths = {}
    seen = set()  # (endpoint_name, method) — avoid double-counting when Flask
                  # registers HEAD alongside GET.

    for rule in app.url_map.iter_rules():
        view_fn = app.view_functions.get(rule.endpoint)
        if view_fn is None:
            continue
        meta = getattr(view_fn, '_openapi_spec', None)
        if meta is None:
            continue
        if meta.get('internal') and not include_internal:
            continue

        oapi_path, path_params = _flask_rule_to_openapi(rule.rule)
        paths.setdefault(oapi_path, {})

        methods = (rule.methods or set()) - {'HEAD', 'OPTIONS'}
        for method in methods:
            key = (rule.endpoint, method)
            if key in seen:
                continue
            seen.add(key)

            op = {
                'operationId': meta.get('operation_id') or rule.endpoint.replace('.', '_'),
                'tags': meta['tags'],
                'parameters': path_params + list(meta['parameters']),
                'responses': _resolve_responses(meta['responses']),
            }
            if meta['summary']:
                op['summary'] = meta['summary']
            if meta['description']:
                op['description'] = meta['description']
            if meta['request_body'] is not None and method in ('POST', 'PUT', 'PATCH'):
                op['requestBody'] = _resolve_request_body(meta['request_body'])
            if meta['security'] is not None:
                op['security'] = meta['security']
            if meta['deprecated']:
                op['deprecated'] = True
            if meta['min_tier']:
                op['x-min-tier'] = meta['min_tier']

            paths[oapi_path][method.lower()] = op

    return paths


__all__ = ['spec', 'build_auto_paths']
