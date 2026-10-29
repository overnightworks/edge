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

# The live catalog, rendered with the mocked provider: its apex redirect stays within the 10 single
# redirects Cloudflare's Free plan allows (https://developers.cloudflare.com/rules/url-forwarding/).
run "the_live_catalogs_apex_redirect_stays_within_the_free_plans_limit" {
  command = plan

  variables {
    catalog_file = "../fleet.yaml"
  }

  assert {
    condition     = length(cloudflare_ruleset.apex_redirect[0].rules) <= 10
    error_message = "the apex redirect has more rules than the 10 single redirects of Cloudflare's Free plan"
  }
}
