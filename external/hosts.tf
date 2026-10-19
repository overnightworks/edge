# The hosts of the enabled catalog entries, keyed by FQDN. src/edge/catalog.py validates the shape
# before tofu starts (`edge external plan` and `apply` refuse an invalid catalog), so this reads it
# as it stands; `try` covers only the optional keys.
locals {
  default_host_target = "http://127.0.0.1:8081"

  hosts = merge([
    for service, entry in try(local.fleet.services, {}) : {
      for host in try(entry.hosts, []) :
      (host.name == "@" ? local.fleet.cloudflare.zone : "${host.name}.${local.fleet.cloudflare.zone}") => {
        service  = service
        exposure = host.exposure
        reason   = try(host.reason, null)
        ready    = try(host.ready, null)
        target   = try(host.target, local.default_host_target)
      }
    } if try(entry.run.enabled, true)
  ]...)
}
