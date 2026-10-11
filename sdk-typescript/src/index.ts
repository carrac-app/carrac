export { Client } from "./client.js";
export {
  // New names
  CarracError,
  AuthError,
  NotFoundError,
  RateLimitError,
  ApiError,
  // Backward-compat aliases; remove in next major version
  ConvexityError,
  ConvexityAuthError,
  ConvexityNotFoundError,
  ConvexityRateLimitError,
  ConvexityApiError,
} from "./errors.js";
export type * from "./types.js";
