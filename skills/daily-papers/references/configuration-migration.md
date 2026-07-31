# Shared configuration migration

This reference is used only when `config_manager.py show` returns
`migration_required=true`.

The unversioned Vault file is a legacy sparse overlay. `show` reconstructs its
old effective settings with the immutable `shared-config-v0-defaults.json`
migration baseline, never the current bundled `defaults.json`. Do not edit an
installed Skill or silently accept the legacy file as current configuration.

For migration, create one unique temporary patch outside the Vault:

```json
{"migration": {"target_schema_version": 1}}
```

Run the ordinary `plan --patch` command and explain that the plan:

- preserves the reconstructed research scope and automation values;
- writes a complete user-owned Vault snapshot with `schema_version: 1`;
- does not adopt current package defaults or change machine-local paths.

Do not run `apply` until the user explicitly approves this migration. Publish it
through the same recoverable `apply --patch` transaction documented by the
configuration workflow. If the user also requested setting changes, migrate
first and apply those changes in a second transaction with a normal patch.
Interrupted migration uses the same original-patch retry or journal-only
`resume` behavior as any other configuration publication.
