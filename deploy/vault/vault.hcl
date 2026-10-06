# Vault server for the VPS: one node, integrated (raft) storage, loopback only.
# Clients on the host use 127.0.0.1:8200; the pricefc pods reach it through pasta's
# host-loopback mapping (169.254.1.2). Plain HTTP is acceptable here because traffic never leaves
# the machine's loopback; anyone able to sniff it is already root on the host.
ui            = false
disable_mlock = true   # recommended with integrated storage; the VPS has no swap
api_addr      = "http://127.0.0.1:8200"
cluster_addr  = "http://127.0.0.1:8201"

storage "raft" {
  path    = "/vault/file"
  node_id = "vps-1"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}
