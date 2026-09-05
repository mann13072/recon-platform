variable "aws_region" {
  description = "AWS region in which the isolated reconciliation stack is deployed."
  type        = string
  default     = "eu-central-1"
}

variable "project" {
  description = "Short project identifier used in resource names."
  type        = string
  default     = "recon"
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  default     = "staging"

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "vpc_cidr" {
  description = "CIDR for the application VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "allowed_ingress_cidrs" {
  description = "CIDRs allowed to reach the public load balancer."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "certificate_arn" {
  description = "ACM certificate ARN used by the HTTPS load-balancer listener."
  type        = string
}

variable "api_image" {
  description = "Immutable API container image, preferably addressed by digest."
  type        = string
}

variable "web_image" {
  description = "Immutable web container image, preferably addressed by digest."
  type        = string
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "web_desired_count" {
  type    = number
  default = 2
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "deletion_protection" {
  description = "Protect durable stores from accidental deletion. Enable in production."
  type        = bool
  default     = true
}
