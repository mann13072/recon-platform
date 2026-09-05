# AWS infrastructure

This Terraform stack provisions the production-shaped AWS foundation: a two-AZ VPC,
private ECS/Fargate API and web services behind an HTTPS ALB, encrypted PostgreSQL and
Redis, an object-locked evidence bucket, KMS, Secrets Manager, and encrypted logs.

The RDS master credential is generated and held by AWS. Populate the `runtime` secret
out of band with `DATABASE_URL`, `AUTH_ISSUER`, `AUTH_AUDIENCE`, `AUTH_JWKS_URL`, and
`CREDENTIAL_ENCRYPTION_KEY` before starting tasks. Do not place those values in tfvars.

```shell
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform fmt -check
terraform validate
terraform plan
```

Never commit `terraform.tfvars` or state, and do not run `terraform apply` from CI.
