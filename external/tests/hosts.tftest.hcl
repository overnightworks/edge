mock_provider "github" {}
mock_provider "cloudflare" {}

run "the_wildcard_catalog_yields_the_expected_hosts" {
  command = plan

  variables {
    catalog_file = "tests/fixtures/hosts-wildcard.yaml"
  }

  assert {
    condition     = local.hosts == jsondecode(file("tests/fixtures/hosts-wildcard.expected.json"))
    error_message = "local.hosts differs from the expectation the Python reader is also held to"
  }
}

run "the_plain_catalog_yields_the_expected_hosts" {
  command = plan

  variables {
    catalog_file = "tests/fixtures/hosts-plain.yaml"
  }

  assert {
    condition     = local.hosts == jsondecode(file("tests/fixtures/hosts-plain.expected.json"))
    error_message = "local.hosts differs from the expectation the Python reader is also held to"
  }
}

run "an_empty_catalog_has_no_hosts" {
  command = plan

  variables {
    catalog_file = "tests/fixtures/empty.yaml"
  }

  assert {
    condition     = length(local.hosts) == 0
    error_message = "the empty fixture has no hosts"
  }
}
