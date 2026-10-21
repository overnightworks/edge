# Cloudflare as found: each host of local.hosts gets a proxied CNAME to the tunnel and, when it
# bypasses or protects, an Access app. Everything is looked up at plan time, so no ID is typed or
# stored twice; live records and apps are imported as found and nothing is converted (D12).
locals {
  operator_group_name = "operator"

  protected_hosts = { for fqdn, host in local.hosts : fqdn => host if host.exposure == "protected" }
  access_app_hosts = {
    for fqdn, host in local.hosts : fqdn => host if contains(["bypass", "protected"], host.exposure)
  }

  zone_id    = data.cloudflare_zone.this.id
  account_id = data.cloudflare_zone.this.account.id
  tunnel_id  = data.cloudflare_zero_trust_tunnel_cloudflared.this.id

  operator_group_ids = [
    for group in flatten(data.cloudflare_zero_trust_access_groups.operator[*].result) : group.id
    if group.name == local.operator_group_name
  ]
  operator_group_id = length(local.operator_group_ids) == 1 ? local.operator_group_ids[0] : null

  # Among these types at a host's name only a single CNAME may exist (D12: no conversion).
  address_record_types = ["A", "AAAA", "CNAME"]
  address_records = {
    for fqdn in keys(local.hosts) : fqdn => [
      for record in data.cloudflare_dns_records.host[fqdn].result : record
      if contains(local.address_record_types, record.type)
    ]
  }
  record_ids_to_import = {
    for fqdn, records in local.address_records : fqdn => "${local.zone_id}/${records[0].id}" if length(records) == 1
  }
  app_ids_to_import = {
    for fqdn in keys(local.access_app_hosts) :
    fqdn => "accounts/${local.account_id}/${data.cloudflare_zero_trust_access_applications.host[fqdn].result[0].id}"
    if length(data.cloudflare_zero_trust_access_applications.host[fqdn].result) == 1
  }
}

# The provider itself fails on no or several matching zones and tunnels (C13). Both are looked up
# with no host too: the tunnel's configuration outlives the last host.
data "cloudflare_zone" "this" {
  filter = { name = local.fleet.cloudflare.zone }
}

data "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  account_id = local.account_id
  filter     = { is_deleted = false }
}

data "cloudflare_dns_records" "host" {
  for_each = local.hosts
  zone_id  = local.zone_id
  name     = { exact = each.key }

  lifecycle {
    postcondition {
      condition = (
        length([for record in self.result : record if record.type != "CNAME" && contains(local.address_record_types, record.type)]) == 0
        && length([for record in self.result : record if record.type == "CNAME"]) <= 1
      )
      error_message = join(" ", [
        "${each.key} already has ${join(", ", [for record in self.result : record.type if contains(local.address_record_types, record.type)])} records.",
        "Only a single CNAME can be adopted; converting a record is not supported (D12)."
      ])
    }
  }
}

# `exact` keeps the apex lookup from matching the wildcard app by substring.
data "cloudflare_zero_trust_access_applications" "host" {
  for_each   = local.access_app_hosts
  account_id = local.account_id
  domain     = each.key
  exact      = true

  lifecycle {
    postcondition {
      condition     = length(self.result) <= 1
      error_message = "${each.key} matches ${length(self.result)} Access apps; exactly one can be adopted."
    }
  }
}

data "cloudflare_zero_trust_access_groups" "operator" {
  count      = length(local.protected_hosts) > 0 ? 1 : 0
  account_id = local.account_id
  name       = local.operator_group_name
}

# The Access policy a host's exposure asks for exists only while a host uses it.
resource "cloudflare_zero_trust_access_policy" "bypass" {
  count      = anytrue([for host in local.access_app_hosts : host.exposure == "bypass"]) ? 1 : 0
  account_id = local.account_id
  name       = "bypass"
  decision   = "bypass"
  include    = [{ everyone = {} }]
}

resource "cloudflare_zero_trust_access_policy" "allow_operator" {
  count      = length(local.protected_hosts) > 0 ? 1 : 0
  account_id = local.account_id
  name       = "allow ${local.operator_group_name}"
  decision   = "allow"
  include    = [{ group = { id = local.operator_group_id } }]

  lifecycle {
    precondition {
      condition     = length(local.operator_group_ids) == 1
      error_message = "A protected host needs exactly one Access group named ${local.operator_group_name}; found ${length(local.operator_group_ids)}."
    }
  }
}

resource "cloudflare_zero_trust_access_application" "host" {
  for_each     = local.access_app_hosts
  account_id   = local.account_id
  name         = each.key
  domain       = each.key
  type         = "self_hosted"
  destinations = [{ type = "public", uri = each.key }]
  policies = [{
    id         = each.value.exposure == "bypass" ? one(cloudflare_zero_trust_access_policy.bypass[*].id) : one(cloudflare_zero_trust_access_policy.allow_operator[*].id)
    precedence = 1
  }]
}

# Protection before reachability: no record exists before the apps do (and none outlives them).
resource "cloudflare_dns_record" "host" {
  for_each   = local.hosts
  zone_id    = local.zone_id
  name       = each.key
  type       = "CNAME"
  content    = "${local.tunnel_id}.cfargotunnel.com"
  proxied    = true
  ttl        = 1
  depends_on = [cloudflare_zero_trust_access_application.host]
}

# HSTS at the zone's edge, for the apex and every subdomain: the preload list is a property of the
# domain. The origins already send nosniff, so it stays off here.
resource "cloudflare_zone_setting" "hsts" {
  zone_id    = local.zone_id
  setting_id = "security_header"
  value = {
    strict_transport_security = {
      enabled            = true
      include_subdomains = true
      max_age            = 31536000
      nosniff            = false
      preload            = true
    }
  }
}

# The visitor's address as Cloudflare saw it, in `Forwarded` (RFC 7239): a header the visitor sent
# is overwritten, which `X-Forwarded-For` cannot be (Cloudflare appends to it and forbids setting
# it). Keycloak reads only this header (KC_PROXY_HEADERS=forwarded). RFC 7239 quotes an IPv6
# address in brackets, so the two families are two rules.
resource "cloudflare_ruleset" "client_address" {
  zone_id = local.zone_id
  name    = "client address"
  kind    = "zone"
  phase   = "http_request_late_transform"
  rules = [
    {
      description = "Forwarded carries the IPv4 address Cloudflare saw"
      action      = "rewrite"
      expression  = "ip.src in {0.0.0.0/0}"
      action_parameters = {
        headers = {
          "Forwarded" = {
            operation  = "set"
            expression = "concat(\"for=\", to_string(ip.src), \";proto=https\")"
          }
        }
      }
    },
    {
      description = "Forwarded carries the IPv6 address Cloudflare saw"
      action      = "rewrite"
      expression  = "ip.src in {::/0}"
      action_parameters = {
        headers = {
          "Forwarded" = {
            operation  = "set"
            expression = "concat(\"for=\\\"[\", to_string(ip.src), \"]\\\";proto=https\")"
          }
        }
      }
    },
  ]
}

import {
  for_each = local.record_ids_to_import
  to       = cloudflare_dns_record.host[each.key]
  id       = each.value
}

import {
  for_each = local.app_ids_to_import
  to       = cloudflare_zero_trust_access_application.host[each.key]
  id       = each.value
}
