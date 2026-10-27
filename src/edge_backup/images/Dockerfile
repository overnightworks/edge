# Never built: the pins of the images `hal backup` runs, one stage each, read by edge_backup.settings
# and kept current by Dependabot's docker ecosystem. Major 17 equals compose.yaml's keycloak-db pin, the sqlite stage its python pin.
FROM restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510 AS restic
FROM postgres:16.15-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea AS restore-16
USER postgres
FROM postgres:17.11-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24 AS restore-17
USER postgres
FROM python:3.13.16-alpine@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a AS sqlite
USER nobody
