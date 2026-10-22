mock_provider "cloudflare" {
  mock_data "cloudflare_zone" {
    defaults = {
      id      = "0123456789abcdef0123456789abcdef"
      account = { id = "account-id", name = "fixture account" }
    }
  }

  mock_data "cloudflare_zero_trust_tunnel_cloudflared" {
    defaults = { id = "tunnel-id" }
  }

  mock_data "cloudflare_zero_trust_access_groups" {
    defaults = {
      result = [{ id = "group-id", name = "operator", is_default = [], include = [], exclude = [], require = [] }]
    }
  }
}

# The live catalog, rendered with the mocked provider: what the plan holds for songmaker's host.
run "the_live_catalog_adds_songmakers_host_with_its_deny" {
  command = plan

  variables {
    catalog_file = "../fleet.yaml"
  }

  assert {
    condition = (
      cloudflare_dns_record.host["songmaker.${local.fleet.cloudflare.zone}"].proxied
      && contains(
        [for rule in cloudflare_zero_trust_tunnel_cloudflared_config.this.config.ingress : rule.hostname == null ? "" : "${rule.hostname}=${rule.service}"],
        "songmaker.${local.fleet.cloudflare.zone}=http://localhost:8080"
      )
    )
    error_message = "songmaker's host has no proxied record or no tunnel line to :8080"
  }

  assert {
    condition = (
      length(cloudflare_ruleset.deny_public) == 1
      && length(cloudflare_ruleset.deny_public[0].rules) == 1
      && alltrue([
        for fqdn in ["songmaker.${local.fleet.cloudflare.zone}", local.fleet.cloudflare.zone, "www.${local.fleet.cloudflare.zone}"] :
        strcontains(cloudflare_ruleset.deny_public[0].rules[0].expression, "starts_with(lower(http.host), \"${fqdn}:\")")
      ])
      && strcontains(cloudflare_ruleset.deny_public[0].rules[0].expression, "/*auth/*backchannel-logout*")
    )
    error_message = "the deny is not one rule for /auth/backchannel-logout on songmaker's host, the apex and www, each with and without a port"
  }

  assert {
    condition = (
      length(cloudflare_dns_record.host) == 4
      && length(cloudflare_zero_trust_tunnel_cloudflared_config.this.config.ingress) == 5
      && !can(cloudflare_zero_trust_access_application.host["songmaker.${local.fleet.cloudflare.zone}"])
    )
    error_message = "the rendered plan does not have 4 records, 5 tunnel lines and no Access app for songmaker's host"
  }
}
