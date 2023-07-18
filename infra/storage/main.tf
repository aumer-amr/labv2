terraform {
  required_providers {
    truenas = {
      source = "dariusbakunas/truenas"
      version = "0.11.1"
    }
    sops = {
      source  = "carlpett/sops"
      version = "0.7.2"
    }
    asserting = {
      source = "kulack/asserting"
      version = "0.0.3"
    }
    http = {
      source = "hashicorp/http"
      version = "3.4.0"
    }
  }
}

data "sops_file" "secrets" {
  source_file = "secret.sops.yaml"
}
