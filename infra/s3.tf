# Bucket names are globally unique across every AWS account, so a fixed name
# fails for the second person who runs this. A random suffix keeps it collision
# free without putting the account ID in the name.
resource "random_id" "suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "logs" {
  bucket = "${var.project_name}-logs-${random_id.suffix.hex}"

  # The stated workflow is `terraform destroy` whenever the project is idle, and
  # destroy fails on a non-empty bucket. Without this you either leak a bucket or
  # empty it by hand every time. It does mean destroy deletes the logs - correct
  # here, where they are disposable, and wrong in production.
  force_destroy = true
}

# Nothing in this bucket is public. Ingested logs are exactly the kind of data
# that ends up in a breach writeup when a bucket is left open.
resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ACLs disabled entirely; access is governed by the IAM policy in iam.tf and
# nothing else. One mechanism is easier to reason about than two.
resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # SSE-S3. Free; SSE-KMS adds per-request charges.
    }
    bucket_key_enabled = true
  }
}

# Versioning is deliberately off. These objects are append-only log batches -
# there is no prior version worth keeping, and versioning would quietly double
# storage against a 5 GB free tier.
resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  # Unbounded retention is how a free-tier bucket turns into a bill.
  rule {
    id     = "expire-ingested-logs"
    status = "Enabled"
    filter {}
    expiration {
      days = var.s3_expire_days
    }
  }

  # Failed uploads leave parts that are invisible in the console but still bill.
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_ownership_controls.logs]
}
