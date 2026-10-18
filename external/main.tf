terraform {
  required_version = "= 1.13.1"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "= 5.28.0"
    }
    github = {
      source  = "integrations/github"
      version = "= 6.13.0"
    }
  }

  # The wrapper supplies the state path with -backend-config=path=...; the state never sits in the checkout.
  backend "local" {}

  encryption {
    key_provider "pbkdf2" "fleet" {
      passphrase = var.state_passphrase
    }

    method "aes_gcm" "fleet" {
      keys = key_provider.pbkdf2.fleet
    }

    state {
      method   = method.aes_gcm.fleet
      enforced = true
    }

    plan {
      method   = method.aes_gcm.fleet
      enforced = true
    }
  }
}

# Passed only as TF_VAR_state_passphrase in the child process's environment.
variable "state_passphrase" {
  type      = string
  sensitive = true
}

# Only the tests set this (external/tests/); the live runs leave it at the real catalog.
variable "catalog_file" {
  type    = string
  default = null
}

locals {
  fleet = yamldecode(file(coalesce(var.catalog_file, "${path.module}/../fleet.yaml")))
}

# The token comes from GITHUB_TOKEN in the child environment; cloudflare reads CLOUDFLARE_API_TOKEN the same way.
provider "github" {
  owner = local.fleet.github.org
}
