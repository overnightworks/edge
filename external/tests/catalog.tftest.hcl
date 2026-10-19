mock_provider "github" {}
mock_provider "cloudflare" {
  mock_data "cloudflare_zero_trust_access_groups" {
    defaults = {
      result = [{ id = "group-id", name = "operator", is_default = [], include = [], exclude = [], require = [] }]
    }
  }
}

variables {
  catalog_file = "tests/fixtures/empty.yaml"
}

run "an_empty_catalog_plans_no_resources" {
  command = plan

  assert {
    condition     = length(local.fleet.services) == 0
    error_message = "the empty fixture has no services"
  }
}
