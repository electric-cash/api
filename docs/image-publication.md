# Image publication

The `Release` workflow publishes container images on pushes to `master`. It
checks out the triggering commit using the job's automatic `GITHUB_TOKEN` with
`contents: read` and does not persist Git credentials. A custom `GH_TOKEN` is
not needed for this workflow. Repository branch protections are unchanged.

The image is built once, labeled with its source SHA, and checked with
`pip check` before cloud credentials are configured. This is not a replacement
for the pull-request regression, HTTP and recovery tests. PR candidate-image
archives are diagnostic artifacts, not a promotion source: release builds the
merged master commit again. Test the actual published artifact before rollout.

## Registry contract

The two configured destinations remain in order:

| Account | Role | Region |
| --- | --- | --- |
| Production: `628422242680` | `github-actions-api-publisher` | `eu-west-1` |
| Staging: `869187294824` | `github-actions-api-publisher` | `eu-west-1` |

Each step assumes its account's role directly using GitHub OIDC, verifies the
returned account ID, and requests a 15-minute session. Both steps explicitly
clear credentials left by preceding steps. No static AWS key secrets, role
chaining, or fallback credentials are used. The historical Singapore mirror
is no longer a publication target. This does not delete its resources, images
or credentials, or establish that no external consumers remain. Review the
destination change before activating this workflow.

The workflow does not provision repositories, OIDC providers or IAM roles.
These must exist before activation; see the IAM contract below. Human CLI
profiles and an interactive `aws login` session do not authenticate Actions.
Do not give the workflow the operator's administrative role or copy temporary
operator credentials into GitHub Secrets.

Both logins complete before the first push; this detects authentication
failures early but does not prove repository-level read/write permissions.
Each account uses a separate temporary Docker configuration, so logins
cannot overwrite one another even if two slots resolve to the same registry.
Each destination receives the full commit-SHA tag followed by `latest` for
backward compatibility. Docker Buildx reads each digest from the registry;
the job summary records the verified SHA digest before attempting the mutable
alias, then checks that `latest` resolves to the same digest. Publication
stops at the first error; it is not atomic across tags or registries. Inspect
the failed step, logs and summary to identify partial publication. Missing a
receipt is not proof that nothing was uploaded, for example if the runner lost
connectivity after a push. There is no automatic deletion or rollback.

Runs are serialized without canceling an in-progress push. GitHub concurrency
may replace a pending run and does not promise FIFO ordering. Do not treat
`latest` as a deployment identifier. Re-running an older workflow can move the
alias backwards; review the source SHA before any rerun. Rebuilding a SHA is
not a guarantee of identical bytes (the base image is not digest-pinned).
Record and verify the registry digest before deployment.

## IAM contract

In each target account, provision an OIDC identity provider for
`https://token.actions.githubusercontent.com`, with the client ID
`sts.amazonaws.com`, and the dedicated role named above. Limit its trust to
`sts:AssumeRoleWithWebIdentity` from that same account's provider, with exact
`StringEquals` conditions:

```json
{
  "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
  "token.actions.githubusercontent.com:sub": "repo:electric-cash/api:ref:refs/heads/master"
}
```

This repository currently uses the default, non-immutable subject format.
Check `GET /repos/electric-cash/api/actions/oidc/customization/sub` before
provisioning or changing trust. An environment-based or immutable-ID subject
requires an intentional trust-policy update, not a wildcard workaround.
No PR, arbitrary branch or tag subject should be allowed.

Grant only `ecr:GetAuthorizationToken` on `*`, and the following actions on
`arn:aws:ecr:eu-west-1:ACCOUNT_ID:repository/api`, substituting the role's own
account ID:

- `ecr:BatchCheckLayerAvailability`
- `ecr:InitiateLayerUpload`
- `ecr:UploadLayerPart`
- `ecr:CompleteLayerUpload`
- `ecr:PutImage`
- `ecr:BatchGetImage`
- `ecr:GetDownloadUrlForLayer`

These cover publication and registry-manifest/config reads. They do not grant
repository creation/deletion, infrastructure deployment, IAM administration
or database access. An IAM simulation and local tests are not proof of a
successful federated session or push. Confirm those in an authorized release
and retain its account, source SHA and digest receipts.

Only the release job requests `id-token: write`; other workflows retain their
existing permissions. This permission applies to the whole job, including
steps before credential configuration. Exact branch trust is not a restriction
to one workflow file: protect `master` and review workflow changes, especially
`pull_request_target` workflows. Never execute untrusted PR code with release
permissions. Existing branch protections must not be relaxed for this repair.

Existing static secrets are no longer read by this workflow. Audit their other
consumers before separately revoking or deleting them. Reverting to the old
workflow would restore its dependency on those credentials, not repair them.

References: [GitHub OIDC in AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)
and [ECR push permissions](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-push-iam.html).

## Versioning and deployment boundaries

Image publication no longer invokes the floating Python Semantic Release
action. It does not bump `setup.py`, write `CHANGELOG.md`, create Git tags or
GitHub releases, or push version commits onto protected `master`. The legacy
`setup.cfg` remains for reference, not as a validated automatic release path.
Any semantic-version release requires a separately reviewed compatible tool
and protected-branch process. Do not re-enable the old action by supplying a
personal token or weakening branch protections.

This workflow contains no deployment, database repair, migration or restart.
Updating registry tags does not change deployments pinned to another SHA or
digest, but consumers following `latest` can pick up the new image. Before
deploying, pull the selected digest, verify its source label and smoke-test the
actual artifact. Follow the separate [backup and rollout procedure](backup-and-rollout.md).

## Local checks

Build the existing test image, then run publication checks without a network,
cloud credentials, a Docker socket or a database:

```bash
docker build -f tests/Dockerfile -t api-cache-tests .
docker run --rm --network none \
  --mount "type=bind,source=$PWD,target=/work,readonly" \
  --entrypoint python api-cache-tests \
  -m unittest discover -s tests/workflows -v
```

These checks parse the workflow and execute the publication script with a fake
Docker command, including failures at each call. They verify local control
flow, not GitHub/AWS authorization or a real registry upload. CI also runs the
application integration suite and image smoke tests.
