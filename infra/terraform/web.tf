# ---- Web: private S3 -> CloudFront (OAC), /api/* -> proxy Lambda -> AgentCore runtime ----
locals {
  ui_dir        = "${path.module}/../../ui"
  ui_files      = fileset(local.ui_dir, "*")
  cognito_host  = "${aws_cognito_user_pool_domain.this.domain}.auth.${var.aws_region}.amazoncognito.com"
  content_types = { html = "text/html; charset=utf-8", css = "text/css; charset=utf-8", js = "text/javascript; charset=utf-8", svg = "image/svg+xml", json = "application/json", png = "image/png" }
}

resource "aws_s3_bucket" "ui" {
  bucket = "${var.name}-ui-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "ui" {
  bucket                  = aws_s3_bucket.ui.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "ui" {
  for_each     = local.ui_files
  bucket       = aws_s3_bucket.ui.id
  key          = each.value
  source       = "${local.ui_dir}/${each.value}"
  etag         = filemd5("${local.ui_dir}/${each.value}")
  content_type = lookup(local.content_types, reverse(split(".", each.value))[0], "application/octet-stream")
}

resource "aws_s3_object" "config" {
  bucket       = aws_s3_bucket.ui.id
  key          = "config.json"
  content_type = "application/json"
  content = jsonencode({
    domain    = local.cognito_host
    client_id = aws_cognito_user_pool_client.ui.id
    api_url   = "/api"
  })
}

resource "aws_cloudfront_origin_access_control" "ui" {
  name                              = "${var.name}-ui"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_iam_policy_document" "ui_bucket" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.ui.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.ui.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "ui" {
  bucket = aws_s3_bucket.ui.id
  policy = data.aws_iam_policy_document.ui_bucket.json
}

resource "aws_ssm_parameter" "runtime_arn" {
  name  = "/${var.name}/runtime-arn"
  type  = "String"
  value = aws_bedrockagentcore_agent_runtime.this.agent_runtime_arn
}

data "aws_iam_policy_document" "bff_ssm" {
  statement {
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.name}/runtime-arn"]
  }
}

resource "aws_iam_role_policy" "bff_ssm" {
  name   = "runtime-arn"
  role   = aws_iam_role.bff.id
  policy = data.aws_iam_policy_document.bff_ssm.json
}

# ---- proxy Lambda (adds no authority; see infra/bff/handler.py) ----
data "archive_file" "bff" {
  type        = "zip"
  source_file = "${path.module}/../bff/handler.py"
  output_path = "${path.module}/.bff.zip"
}

data "aws_iam_policy_document" "bff_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "bff" {
  name               = "${var.name}-bff"
  assume_role_policy = data.aws_iam_policy_document.bff_trust.json
}

resource "aws_iam_role_policy_attachment" "bff_logs" {
  role       = aws_iam_role.bff.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_cloudwatch_log_group" "bff" {
  name              = "/aws/lambda/${var.name}-bff"
  retention_in_days = 30
}

resource "aws_lambda_function" "bff" {
  function_name    = "${var.name}-bff"
  role             = aws_iam_role.bff.arn
  runtime          = "python3.12"
  handler          = "handler.handler"
  filename         = data.archive_file.bff.output_path
  source_code_hash = data.archive_file.bff.output_base64sha256
  timeout          = 120
  memory_size      = 256

  environment {
    variables = { RUNTIME_ARN_PARAM = "/${var.name}/runtime-arn" }
  }

  depends_on = [aws_cloudwatch_log_group.bff]
}

# Auth NONE is deliberate: CloudFront cannot sign requests that already carry the user's Authorization header.
# The JWT is verified by AgentCore and again by the app, and the handler narrows the surface to one path.
resource "aws_lambda_function_url" "bff" {
  function_name      = aws_lambda_function.bff.function_name
  authorization_type = "NONE"
}

# ---- security headers ----
resource "aws_cloudfront_response_headers_policy" "ui" {
  name = "${var.name}-ui-security"

  security_headers_config {
    content_type_options {
      override = true
    }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "no-referrer"
      override        = true
    }
    strict_transport_security {
      access_control_max_age_sec = 63072000
      include_subdomains         = true
      preload                    = true
      override                   = true
    }
    content_security_policy {
      override = true
      content_security_policy = join("; ", [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src https://fonts.gstatic.com",
        "img-src 'self' data:",
        "connect-src 'self' https://${local.cognito_host}",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
      ])
    }
  }
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_distribution" "ui" {
  enabled             = true
  comment             = "${var.name} UI"
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  http_version        = "http2and3"

  origin {
    origin_id                = "ui"
    domain_name              = aws_s3_bucket.ui.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.ui.id
  }

  origin {
    origin_id   = "api"
    domain_name = replace(replace(aws_lambda_function_url.bff.function_url, "https://", ""), "/", "")

    custom_origin_config {
      http_port                = 80
      https_port               = 443
      origin_protocol_policy   = "https-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }
  }

  default_cache_behavior {
    target_origin_id           = "ui"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.ui.id
  }

  ordered_cache_behavior {
    path_pattern               = "/api/*"
    target_origin_id           = "api"
    viewer_protocol_policy     = "https-only"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.ui.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

output "ui_url" {
  value = "https://${aws_cloudfront_distribution.ui.domain_name}"
}
