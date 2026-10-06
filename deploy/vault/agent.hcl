# Vault Agent for one environment pod (deploy/quadlet/pricefc-vault-agent.container).
# Logs in with the environment's AppRole and renders its secrets as one file each into a tmpfs
# directory that the other containers mount read-only. When Vault is sealed or unreachable the
# agent keeps retrying and the last rendered files stay in place.
#
# Installed by deploy.sh as ~/.config/pricefc/vault/agent.hcl, next to role-id and secret-id
# (mode 600, issued by deploy/vault/issue-approle.sh).

vault {
  # Host loopback, reachable from the pod through pasta --map-host-loopback (pricefc.pod).
  address = "http://169.254.1.2:8200"
  retry {
    num_retries = -1
  }
}

auto_auth {
  method "approle" {
    config = {
      role_id_file_path                   = "/vault/agent/role-id"
      secret_id_file_path                 = "/vault/agent/secret-id"
      remove_secret_id_file_after_reading = false
    }
  }
}

template_config {
  exit_on_retry_failure         = false
  static_secret_render_interval = "5m"
}

# secret/pricefc/<env>/telegram: bot_token, chat_id
template {
  destination = "/secrets/telegram_bot_token"
  perms       = "0400"
  contents    = <<-EOT
  {{- with secret (printf "secret/data/pricefc/%s/telegram" (env "PRICEFC_ENV")) }}{{ .Data.data.bot_token }}{{ end -}}
  EOT
}

template {
  destination = "/secrets/telegram_chat_id"
  perms       = "0400"
  contents    = <<-EOT
  {{- with secret (printf "secret/data/pricefc/%s/telegram" (env "PRICEFC_ENV")) }}{{ .Data.data.chat_id }}{{ end -}}
  EOT
}
