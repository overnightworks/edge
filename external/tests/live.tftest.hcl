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

# The live catalog, rendered with the mocked provider: songmaker's host with its deny, and the
# apex and www as proxied records that redirect at the edge; only the apex, as legal's host, has a tunnel line.
run "the_live_catalog_renders_songmakers_host_with_its_deny_and_the_apex_redirect" {
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
      cloudflare_dns_record.host["hopin.${local.fleet.cloudflare.zone}"].proxied
      && contains(
        [for rule in cloudflare_zero_trust_tunnel_cloudflared_config.this.config.ingress : rule.hostname == null ? "" : "${rule.hostname}=${rule.service}"],
        "hopin.${local.fleet.cloudflare.zone}=http://127.0.0.1:8081"
      )
    )
    error_message = "hopin's host has no proxied record or no tunnel line to :8081"
  }

  assert {
    condition = (
      length(cloudflare_ruleset.deny_public) == 1
      && length(cloudflare_ruleset.deny_public[0].rules) == 2
      && alltrue([
        for rule in cloudflare_ruleset.deny_public[0].rules :
        strcontains(rule.expression, "lower(http.host) eq \"songmaker.${local.fleet.cloudflare.zone}\"")
        && !strcontains(rule.expression, "www.")
      ])
      && strcontains(cloudflare_ruleset.deny_public[0].rules[0].expression, "/*auth/*backchannel-logout*")
      && strcontains(cloudflare_ruleset.deny_public[0].rules[0].expression, "lower(http.host) eq \"hopin.${local.fleet.cloudflare.zone}\"")
      && strcontains(cloudflare_ruleset.deny_public[0].rules[1].expression, "/*metrics*")
      && !strcontains(cloudflare_ruleset.deny_public[0].rules[1].expression, "hopin.")
    )
    error_message = "the deny is not two rules: /auth/backchannel-logout on songmaker's and hopin's host, /metrics on songmaker's alone"
  }

  assert {
    condition = (
      length(cloudflare_dns_record.host) == 5
      && length(cloudflare_zero_trust_tunnel_cloudflared_config.this.config.ingress) == 5
      && alltrue([for fqdn in [local.fleet.cloudflare.zone, "www.${local.fleet.cloudflare.zone}"] : cloudflare_dns_record.host[fqdn].proxied])
      && !can(cloudflare_zero_trust_access_application.host["songmaker.${local.fleet.cloudflare.zone}"])
    )
    error_message = "the rendered plan does not have 5 proxied records and 5 tunnel lines (songmaker, hopin, the apex, auth, the 404), with www only as a record"
  }

  assert {
    condition = (
      [for rule in cloudflare_ruleset.apex_redirect[0].rules : rule.action_parameters.from_value.target_url.value] == [
        "https://songmaker.${local.fleet.cloudflare.zone}/impressum",
        "https://songmaker.${local.fleet.cloudflare.zone}/datenschutz",
        "https://${local.fleet.cloudflare.zone}/nutzungsbedingungen",
        "https://auth.${local.fleet.cloudflare.zone}/",
      ]
      && alltrue([
        for rule in cloudflare_ruleset.apex_redirect[0].rules :
        rule.action_parameters.from_value.status_code == 302 && !rule.action_parameters.from_value.preserve_query_string
      ])
      && length(cloudflare_ruleset.apex_redirect[0].rules) <= 10
      && strcontains(cloudflare_ruleset.apex_redirect[0].rules[3].expression, "starts_with(http.request.uri.path, \"/_rechtliches/\")")
      && alltrue([
        for rule in cloudflare_ruleset.apex_redirect[0].rules :
        startswith(rule.expression, "ssl and ") && strcontains(rule.expression, "lower(http.host) eq \"${local.fleet.cloudflare.zone}\"") && strcontains(rule.expression, "starts_with(lower(http.host), \"www.${local.fleet.cloudflare.zone}:\")")
      ])
    )
    error_message = "the apex redirect is not /impressum and /datenschutz to songmaker, the terms page to the apex, then everything else but the apex's served paths to auth, 302 without the query, for the apex and www"
  }
}
