mock_provider "cloudflare" {}

variables {
  catalog_file = "tests/fixtures/empty.yaml"
}

run "an_empty_catalog_has_no_services" {
  command = plan

  assert {
    condition     = length(local.fleet.services) == 0
    error_message = "the empty fixture has no services"
  }
}
