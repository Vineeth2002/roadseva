import os

ENV = os.getenv("ENVIRONMENT", "development")

VALID_ENVS = {"development", "production", "testing"}
if ENV not in VALID_ENVS:
    raise RuntimeError(f"Invalid ENVIRONMENT '{ENV}'. Must be one of: {VALID_ENVS}")

DATABASE_URL = os.getenv("DATABASE_URL")          # None = SQLite fallback
REDIS_URL    = os.getenv("REDIS_URL")             # None = in-memory fallback
SECRET_KEY   = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")

S3_BUCKET = os.getenv("S3_BUCKET")
S3_REGION = os.getenv("S3_REGION", "ap-south-1")

# Third-party service keys (optional — features degrade gracefully if absent)
GROQ_API_KEY      = os.getenv("GROQ_API_KEY")
TWILIO_SID        = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN      = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM       = os.getenv("TWILIO_FROM_NUMBER")
CLOUDINARY_URL    = os.getenv("CLOUDINARY_URL")

# Warn (don't crash) in production if critical vars are missing
if ENV == "production":
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required in production")
    if SECRET_KEY == "dev-secret-key-change-in-production":
        raise RuntimeError("SECRET_KEY must be set in production")