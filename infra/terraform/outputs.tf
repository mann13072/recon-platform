output "application_url" {
  description = "HTTPS endpoint for the reconciliation control room."
  value       = "https://${aws_lb.main.dns_name}"
}

output "evidence_bucket" {
  value = aws_s3_bucket.evidence.id
}

output "database_master_secret_arn" {
  value     = aws_db_instance.postgres.master_user_secret[0].secret_arn
  sensitive = true
}

output "runtime_secret_arn" {
  value     = aws_secretsmanager_secret.runtime.arn
  sensitive = true
}
