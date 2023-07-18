provider "truenas" {
  api_key = data.sops_file.secrets.data["truenas_apikey"]
  base_url = data.sops_file.secrets.data["truenas_baseurl"]
}
