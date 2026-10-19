mock_provider "github" {}

mock_provider "cloudflare" {
  mock_data "cloudflare_zone" {
    defaults = {
      id      = "zone-id"
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

variables {
  catalog_file = "tests/fixtures/hosts-plain.yaml"
}

run "c1_public_hosts_get_proxied_cnames_to_the_tunnel_and_no_app" {
  command = plan

  assert {
    condition = alltrue([
      for fqdn in ["edge-fixture.invalid", "www.edge-fixture.invalid"] :
      cloudflare_dns_record.host[fqdn].name == fqdn
      && cloudflare_dns_record.host[fqdn].type == "CNAME"
      && cloudflare_dns_record.host[fqdn].content == "tunnel-id.cfargotunnel.com"
      && cloudflare_dns_record.host[fqdn].proxied
      && cloudflare_dns_record.host[fqdn].ttl == 1
      && cloudflare_dns_record.host[fqdn].zone_id == "zone-id"
    ])
    error_message = "the apex and www are not proxied CNAMEs to the tunnel with TTL 1"
  }

  assert {
    condition     = length(cloudflare_dns_record.host) == 2
    error_message = "a disabled entry's host got a record"
  }

  assert {
    condition     = length(cloudflare_zero_trust_access_application.host) == 0 && length(cloudflare_zero_trust_access_policy.bypass) == 0 && length(cloudflare_zero_trust_access_policy.allow_operator) == 0
    error_message = "public hosts got an app or a policy"
  }
}

run "c3_a_bypass_host_gets_a_record_an_app_and_the_bypass_policy" {
  command = plan

  override_resource {
    target = cloudflare_zero_trust_access_policy.bypass[0]
    values = { id = "bypass-policy-id" }
  }

  variables {
    catalog_file = "tests/fixtures/cloudflare-bypass.yaml"
  }

  assert {
    condition     = length(cloudflare_dns_record.host) == 1 && length(cloudflare_zero_trust_access_application.host) == 1 && length(cloudflare_zero_trust_access_policy.bypass) == 1
    error_message = "expected one record, one app and one Bypass policy"
  }

  assert {
    condition     = cloudflare_zero_trust_access_policy.bypass[0].decision == "bypass" && length(cloudflare_zero_trust_access_policy.allow_operator) == 0
    error_message = "the policy is not the Bypass policy, or an allow policy exists too"
  }

  assert {
    condition     = cloudflare_zero_trust_access_application.host["hopin.edge-fixture.invalid"].domain == "hopin.edge-fixture.invalid"
    error_message = "the app is not on the host's FQDN"
  }

  assert {
    condition = (
      [for policy in cloudflare_zero_trust_access_application.host["hopin.edge-fixture.invalid"].policies : policy.id] == ["bypass-policy-id"]
      && [for entry in cloudflare_zero_trust_access_policy.bypass[0].include : entry.everyone != null] == [true]
    )
    error_message = "the app does not attach the Bypass policy, or that policy does not include everyone"
  }
}

run "c4_a_host_of_a_disabled_entry_gets_nothing" {
  command = plan

  assert {
    condition     = !contains(keys(cloudflare_dns_record.host), "gone.edge-fixture.invalid")
    error_message = "the disabled entry's host got a record"
  }
}

run "c5_a_protected_wildcard_gets_an_allow_group_app" {
  command = plan

  override_resource {
    target = cloudflare_zero_trust_access_policy.allow_operator[0]
    values = { id = "allow-policy-id" }
  }

  variables {
    catalog_file = "tests/fixtures/cloudflare-protected.yaml"
  }

  assert {
    condition     = cloudflare_zero_trust_access_policy.allow_operator[0].decision == "allow" && [for entry in cloudflare_zero_trust_access_policy.allow_operator[0].include : entry.group.id] == ["group-id"]
    error_message = "the protected host's policy does not allow the operator group"
  }

  assert {
    condition     = length(cloudflare_zero_trust_access_application.host) == 1 && length(cloudflare_zero_trust_access_policy.bypass) == 0
    error_message = "expected one app for the wildcard and no Bypass policy"
  }

  assert {
    condition     = [for policy in cloudflare_zero_trust_access_application.host["*.edge-fixture.invalid"].policies : policy.id] == ["allow-policy-id"]
    error_message = "the wildcard app does not attach the allow-group policy"
  }
}

run "c5_a_protected_host_without_the_operator_group_fails" {
  command = plan

  variables {
    catalog_file = "tests/fixtures/cloudflare-protected.yaml"
  }

  override_data {
    target = data.cloudflare_zero_trust_access_groups.operator[0]
    values = { result = [] }
  }

  expect_failures = [cloudflare_zero_trust_access_policy.allow_operator]
}

run "c15_a_missing_operator_group_only_warns_while_no_host_is_protected" {
  command = plan

  override_data {
    target = data.cloudflare_zero_trust_access_groups.inventory
    values = { result = [] }
  }

  expect_failures = [check.operator_group_exists]
}

run "c16_existing_access_apps_make_the_inventory_warn" {
  command = plan

  override_data {
    target = data.cloudflare_zero_trust_access_applications.inventory
    values = {
      result = [{ id = "other-app", domain = "other.edge-fixture.invalid", allow_authenticate_via_warp = null, allow_iframe = null, allowed_idps = null, app_launcher_logo_url = null, app_launcher_visible = null, aud = null, auto_redirect_to_identity = null, bg_color = null, cors_headers = null, custom_deny_message = null, custom_deny_url = null, custom_non_identity_deny_url = null, custom_pages = null, destinations = null, enable_binding_cookie = null, footer_links = null, header_bg_color = null, http_only_cookie_attribute = null, landing_page_design = null, logo_url = null, mfa_config = null, name = null, options_preflight_bypass = null, path_cookie_attribute = null, policies = null, read_service_tokens_from_header = null, saas_app = null, same_site_cookie_attribute = null, scim_config = null, self_hosted_domains = null, service_auth_401_redirect = null, session_duration = null, skip_app_launcher_login_page = null, skip_interstitial = null, tags = null, target_criteria = null, type = null }]
    }
  }

  expect_failures = [check.access_apps_inventory]
}

run "c16_an_app_without_a_domain_still_makes_the_inventory_warn" {
  command = plan

  override_data {
    target = data.cloudflare_zero_trust_access_applications.inventory
    values = {
      result = [{ id = "infra-app", allow_authenticate_via_warp = null, allow_iframe = null, allowed_idps = null, app_launcher_logo_url = null, app_launcher_visible = null, aud = null, auto_redirect_to_identity = null, bg_color = null, cors_headers = null, custom_deny_message = null, custom_deny_url = null, custom_non_identity_deny_url = null, custom_pages = null, destinations = null, domain = null, enable_binding_cookie = null, footer_links = null, header_bg_color = null, http_only_cookie_attribute = null, landing_page_design = null, logo_url = null, mfa_config = null, name = null, options_preflight_bypass = null, path_cookie_attribute = null, policies = null, read_service_tokens_from_header = null, saas_app = null, same_site_cookie_attribute = null, scim_config = null, self_hosted_domains = null, service_auth_401_redirect = null, session_duration = null, skip_app_launcher_login_page = null, skip_interstitial = null, tags = null, target_criteria = null, type = null }]
    }
  }

  expect_failures = [check.access_apps_inventory]
}

run "c5_two_operator_groups_fail_a_protected_host" {
  command = plan

  variables {
    catalog_file = "tests/fixtures/cloudflare-protected.yaml"
  }

  override_data {
    target = data.cloudflare_zero_trust_access_groups.operator[0]
    values = {
      result = [
        { id = "first-group", name = "operator", is_default = [], include = [], exclude = [], require = [] },
        { id = "second-group", name = "operator", is_default = [], include = [], exclude = [], require = [] },
      ]
    }
  }

  expect_failures = [cloudflare_zero_trust_access_policy.allow_operator]
}

run "c7_a_live_a_record_on_the_apex_fails_the_plan" {
  command = plan

  override_data {
    target = data.cloudflare_dns_records.host["edge-fixture.invalid"]
    values = {
      result = [{ id = "live-a", type = "A", comment = null, comment_modified_on = null, content = null, created_on = null, data = null, meta = null, modified_on = null, name = null, priority = null, private_routing = null, proxiable = null, proxied = null, settings = null, tags = null, tags_modified_on = null, ttl = null }]
    }
  }

  expect_failures = [data.cloudflare_dns_records.host]
}

run "c7_a_live_aaaa_record_on_the_apex_fails_the_plan" {
  command = plan

  override_data {
    target = data.cloudflare_dns_records.host["edge-fixture.invalid"]
    values = {
      result = [{ id = "live-aaaa", type = "AAAA", comment = null, comment_modified_on = null, content = null, created_on = null, data = null, meta = null, modified_on = null, name = null, priority = null, private_routing = null, proxiable = null, proxied = null, settings = null, tags = null, tags_modified_on = null, ttl = null }]
    }
  }

  expect_failures = [data.cloudflare_dns_records.host]
}

run "c7_two_live_cnames_on_www_fail_the_plan" {
  command = plan

  override_data {
    target = data.cloudflare_dns_records.host["www.edge-fixture.invalid"]
    values = {
      result = [
        { id = "first", type = "CNAME", comment = null, comment_modified_on = null, content = null, created_on = null, data = null, meta = null, modified_on = null, name = null, priority = null, private_routing = null, proxiable = null, proxied = null, settings = null, tags = null, tags_modified_on = null, ttl = null },
        { id = "second", type = "CNAME", comment = null, comment_modified_on = null, content = null, created_on = null, data = null, meta = null, modified_on = null, name = null, priority = null, private_routing = null, proxiable = null, proxied = null, settings = null, tags = null, tags_modified_on = null, ttl = null },
      ]
    }
  }

  expect_failures = [data.cloudflare_dns_records.host]
}

run "c9_mx_and_txt_records_at_the_apex_are_ignored" {
  command = plan

  override_data {
    target = data.cloudflare_dns_records.host["edge-fixture.invalid"]
    values = {
      result = [
        { id = "mx", type = "MX", comment = null, comment_modified_on = null, content = null, created_on = null, data = null, meta = null, modified_on = null, name = null, priority = null, private_routing = null, proxiable = null, proxied = null, settings = null, tags = null, tags_modified_on = null, ttl = null },
        { id = "txt", type = "TXT", comment = null, comment_modified_on = null, content = null, created_on = null, data = null, meta = null, modified_on = null, name = null, priority = null, private_routing = null, proxiable = null, proxied = null, settings = null, tags = null, tags_modified_on = null, ttl = null },
      ]
    }
  }

  assert {
    condition     = length(local.record_ids_to_import) == 0 && contains(keys(cloudflare_dns_record.host), "edge-fixture.invalid")
    error_message = "other record types were imported, or the apex record is not planned"
  }
}

run "c11_two_apps_for_a_bypass_host_fail_the_plan" {
  command = plan

  variables {
    catalog_file = "tests/fixtures/cloudflare-bypass.yaml"
  }

  override_data {
    target = data.cloudflare_zero_trust_access_applications.host["hopin.edge-fixture.invalid"]
    values = {
      result = [
        { id = "first", allow_authenticate_via_warp = null, allow_iframe = null, allowed_idps = null, app_launcher_logo_url = null, app_launcher_visible = null, aud = null, auto_redirect_to_identity = null, bg_color = null, cors_headers = null, custom_deny_message = null, custom_deny_url = null, custom_non_identity_deny_url = null, custom_pages = null, destinations = null, domain = null, enable_binding_cookie = null, footer_links = null, header_bg_color = null, http_only_cookie_attribute = null, landing_page_design = null, logo_url = null, mfa_config = null, name = null, options_preflight_bypass = null, path_cookie_attribute = null, policies = null, read_service_tokens_from_header = null, saas_app = null, same_site_cookie_attribute = null, scim_config = null, self_hosted_domains = null, service_auth_401_redirect = null, session_duration = null, skip_app_launcher_login_page = null, skip_interstitial = null, tags = null, target_criteria = null, type = null },
        { id = "second", allow_authenticate_via_warp = null, allow_iframe = null, allowed_idps = null, app_launcher_logo_url = null, app_launcher_visible = null, aud = null, auto_redirect_to_identity = null, bg_color = null, cors_headers = null, custom_deny_message = null, custom_deny_url = null, custom_non_identity_deny_url = null, custom_pages = null, destinations = null, domain = null, enable_binding_cookie = null, footer_links = null, header_bg_color = null, http_only_cookie_attribute = null, landing_page_design = null, logo_url = null, mfa_config = null, name = null, options_preflight_bypass = null, path_cookie_attribute = null, policies = null, read_service_tokens_from_header = null, saas_app = null, same_site_cookie_attribute = null, scim_config = null, self_hosted_domains = null, service_auth_401_redirect = null, session_duration = null, skip_app_launcher_login_page = null, skip_interstitial = null, tags = null, target_criteria = null, type = null },
      ]
    }
  }

  expect_failures = [data.cloudflare_zero_trust_access_applications.host]
}

run "c1_one_live_cname_on_www_is_imported" {
  command = plan

  plan_options {
    mode = refresh-only
  }

  override_data {
    target = data.cloudflare_dns_records.host["www.edge-fixture.invalid"]
    values = {
      result = [{ id = "cname-id", type = "CNAME", comment = null, comment_modified_on = null, content = null, created_on = null, data = null, meta = null, modified_on = null, name = null, priority = null, private_routing = null, proxiable = null, proxied = null, settings = null, tags = null, tags_modified_on = null, ttl = null }]
    }
  }

  assert {
    condition     = local.record_ids_to_import == { "www.edge-fixture.invalid" = "zone-id/cname-id" }
    error_message = "the live CNAME on www is not imported by its zone and record id"
  }
}

run "c10_a_live_cname_with_other_content_is_imported_without_failure" {
  command = plan

  plan_options {
    mode = refresh-only
  }

  override_data {
    target = data.cloudflare_dns_records.host["www.edge-fixture.invalid"]
    values = {
      result = [{ id = "cname-id", type = "CNAME", comment = null, comment_modified_on = null, content = "elsewhere.invalid", created_on = null, data = null, meta = null, modified_on = null, name = null, priority = null, private_routing = null, proxiable = null, proxied = false, settings = null, tags = null, tags_modified_on = null, ttl = 300 }]
    }
  }

  assert {
    condition     = local.record_ids_to_import == { "www.edge-fixture.invalid" = "zone-id/cname-id" }
    error_message = "a CNAME with other content was not adopted, or it failed the plan"
  }
}

run "c11_one_app_for_a_bypass_host_is_imported" {
  command = plan

  variables {
    catalog_file = "tests/fixtures/cloudflare-bypass.yaml"
  }

  plan_options {
    mode = refresh-only
  }

  override_data {
    target = data.cloudflare_zero_trust_access_applications.host["hopin.edge-fixture.invalid"]
    values = {
      result = [{ id = "app-id", allow_authenticate_via_warp = null, allow_iframe = null, allowed_idps = null, app_launcher_logo_url = null, app_launcher_visible = null, aud = null, auto_redirect_to_identity = null, bg_color = null, cors_headers = null, custom_deny_message = null, custom_deny_url = null, custom_non_identity_deny_url = null, custom_pages = null, destinations = null, domain = "hopin.edge-fixture.invalid", enable_binding_cookie = null, footer_links = null, header_bg_color = null, http_only_cookie_attribute = null, landing_page_design = null, logo_url = null, mfa_config = null, name = null, options_preflight_bypass = null, path_cookie_attribute = null, policies = null, read_service_tokens_from_header = null, saas_app = null, same_site_cookie_attribute = null, scim_config = null, self_hosted_domains = null, service_auth_401_redirect = null, session_duration = null, skip_app_launcher_login_page = null, skip_interstitial = null, tags = null, target_criteria = null, type = null }]
    }
  }

  assert {
    condition     = local.app_ids_to_import == { "hopin.edge-fixture.invalid" = "accounts/account-id/app-id" }
    error_message = "the live app is not imported by its account and app id"
  }
}

run "c11_no_app_for_a_bypass_host_is_imported_so_one_is_created" {
  command = plan

  variables {
    catalog_file = "tests/fixtures/cloudflare-bypass.yaml"
  }

  assert {
    condition     = length(local.app_ids_to_import) == 0 && length(cloudflare_zero_trust_access_application.host) == 1
    error_message = "the missing app should be planned, not imported"
  }
}

run "c12_without_hosts_nothing_is_looked_up" {
  command = plan

  variables {
    catalog_file = "tests/fixtures/empty.yaml"
  }

  assert {
    condition = alltrue([
      length(data.cloudflare_zone.this) == 0,
      length(data.cloudflare_zero_trust_tunnel_cloudflared.this) == 0,
      length(data.cloudflare_dns_records.host) == 0,
      length(data.cloudflare_zero_trust_access_applications.host) == 0,
      length(data.cloudflare_zero_trust_access_groups.operator) == 0,
    ])
    error_message = "a lookup was read although no host is declared"
  }
}

run "x3_apply_the_public_hosts" {
  command = apply
}

run "x3_a_changed_tunnel_updates_every_record_content" {
  command = plan

  override_data {
    target = data.cloudflare_zero_trust_tunnel_cloudflared.this[0]
    values = { id = "new-tunnel-id" }
  }

  assert {
    condition = alltrue([
      for record in cloudflare_dns_record.host : record.content == "new-tunnel-id.cfargotunnel.com"
    ])
    error_message = "a record does not follow the changed tunnel"
  }
}

run "c17_apply_a_bypass_host" {
  command = apply

  variables {
    catalog_file = "tests/fixtures/cloudflare-bypass.yaml"
  }

  assert {
    condition     = length(cloudflare_dns_record.host) == 1 && length(cloudflare_zero_trust_access_application.host) == 1
    error_message = "the host was not applied"
  }
}

run "c17_the_removed_host_leaves_the_plan" {
  command = plan

  variables {
    catalog_file = "tests/fixtures/empty.yaml"
  }

  assert {
    condition     = length(cloudflare_dns_record.host) == 0 && length(cloudflare_zero_trust_access_application.host) == 0 && length(cloudflare_zero_trust_access_policy.bypass) == 0
    error_message = "the removed host's objects are still planned"
  }
}
