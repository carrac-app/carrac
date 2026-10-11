"""Allow running as: python -m morning_engine"""
import os
import sentry_sdk
if dsn := os.environ.get('SENTRY_DSN'):
    sentry_sdk.init(
        dsn=dsn,
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
        send_default_pii=False,
        environment=os.environ.get('SENTRY_ENV', 'production'),
        release=os.environ.get('APP_VERSION', 'unknown'),
    )
    sentry_sdk.set_tag('surface', 'morning_engine')

from .engine import main
main()
