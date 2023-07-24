# Check for pools
data "truenas_pool_ids" "all" {}

output "pool_ids" {
  value = "Found ${length(data.truenas_pool_ids.all.ids)} pools"
}

data "asserting_test" "pool_ids" {
    test = length(data.truenas_pool_ids.all.ids) > 0
    throw = "There should be at least one pool, found ${length(data.truenas_pool_ids.all.ids)}"
}

# Check for users
data "http" "check_k8s_user" {
  url = "${data.sops_file.secrets.data["truenas_baseurl"]}/user?username=${data.sops_file.secrets.data["truenas_mapall_user"]}"
  method = "GET"

  request_headers = {
    Authorization = "Bearer ${data.sops_file.secrets.data["truenas_apikey"]}"
  }

  lifecycle {
    postcondition {
      condition     = contains([200], self.status_code)
      error_message = "Status code invalid"
    }
  }
}

data "asserting_test" "k8s_user" {
    test = length(jsondecode(data.http.check_k8s_user.response_body)) > 0
    throw = "User ${data.sops_file.secrets.data["truenas_mapall_user"]} not found"
}

# Datasets
variable "datasets" {
  type = list(string)
  default = ["k8s-pvc", "pve-isos", "pve-backups"]
}

resource "truenas_dataset" "dataset-creation" {
  for_each = toset(var.datasets)
  pool = var.truenas_pool_name
  name = each.key

  # lifecycle {
  #   prevent_destroy = true
  # }
}

# NFS shares
resource "truenas_share_nfs" "nfs-creation" {
  for_each = toset(var.datasets)
  alldirs = true
  paths = ["/mnt/${var.truenas_pool_name}/${each.key}"]
  mapall_user = data.sops_file.secrets.data["truenas_mapall_user"]
  mapall_group = data.sops_file.secrets.data["truenas_mapall_group"]

  depends_on = [ truenas_dataset.dataset-creation ]
}

# Change ACL
data "http" "set_acl" {
  for_each = toset(var.datasets)
  url = "${data.sops_file.secrets.data["truenas_baseurl"]}/filesystem/setacl?id=${each.key}"
  method = "POST"

  request_body = <<EOF
  {
    "path": "/mnt/${var.truenas_pool_name}/${each.key}",
    "dacl": [
      {
          "tag": "owner@",
          "id": null,
          "perms": {
              "BASIC": "FULL_CONTROL"
          },
          "flags": {
              "BASIC": "INHERIT"
          },
          "type": "ALLOW"
      },
      {
          "tag": "group@",
          "id": null,
          "perms": {
              "BASIC": "FULL_CONTROL"
          },
          "flags": {
              "BASIC": "INHERIT"
          },
          "type": "ALLOW"
      },
      {
          "tag": "everyone@",
          "id": null,
          "perms": {
              "BASIC": "MODIFY"
          },
          "flags": {
              "BASIC": "INHERIT"
          },
          "type": "ALLOW"
      }
    ]
  }
  EOF

  request_headers = {
    Authorization = "Bearer ${data.sops_file.secrets.data["truenas_apikey"]}"
  }

  depends_on = [ truenas_dataset.dataset-creation ]

  lifecycle {
    postcondition {
      condition     = contains([200], self.status_code)
      error_message = "Status code invalid"
    }
  }
}
