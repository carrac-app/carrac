export class CarracError extends Error {
  statusCode?: number;
  response?: unknown;
  constructor(message: string, statusCode?: number, response?: unknown) {
    super(message);
    this.name = "CarracError";
    this.statusCode = statusCode;
    this.response = response;
  }
}

export class AuthError extends CarracError {
  constructor(message: string, statusCode?: number, response?: unknown) {
    super(message, statusCode, response);
    this.name = "AuthError";
  }
}

export class NotFoundError extends CarracError {
  constructor(message: string, statusCode?: number, response?: unknown) {
    super(message, statusCode, response);
    this.name = "NotFoundError";
  }
}

export class RateLimitError extends CarracError {
  retryAfter: number;
  constructor(message: string, retryAfter = 0, response?: unknown) {
    super(message, 429, response);
    this.name = "RateLimitError";
    this.retryAfter = retryAfter;
  }
}

export class ApiError extends CarracError {
  constructor(message: string, statusCode?: number, response?: unknown) {
    super(message, statusCode, response);
    this.name = "ApiError";
  }
}

// Backward-compat aliases; remove in next major version
/** @deprecated Use CarracError. */
export const ConvexityError = CarracError;
/** @deprecated Use AuthError. */
export const ConvexityAuthError = AuthError;
/** @deprecated Use NotFoundError. */
export const ConvexityNotFoundError = NotFoundError;
/** @deprecated Use RateLimitError. */
export const ConvexityRateLimitError = RateLimitError;
/** @deprecated Use ApiError. */
export const ConvexityApiError = ApiError;
