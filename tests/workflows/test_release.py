"""Publication contract and shell tests; no cloud credentials or Docker required."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
IMAGE = 'example.invalid/api'


class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.load(
            (ROOT / '.github/workflows/release.yml').read_text(),
            Loader=yaml.BaseLoader,
        )
        cls.job = cls.workflow['jobs']['release']
        cls.steps = cls.job['steps']

    def test_only_master_pushes_can_publish(self):
        self.assertEqual(self.workflow['on'], {'push': {'branches': ['master']}})

    def test_job_token_is_read_only_and_not_persisted(self):
        self.assertEqual(self.workflow.get('permissions'), {'contents': 'read'})
        self.assertNotIn('permissions', self.job)
        checkout = next(step for step in self.steps
                        if step.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(checkout['with'].get('ref'), '${{ github.sha }}')
        self.assertEqual(checkout['with'].get('persist-credentials'), 'false')
        self.assertNotIn('token', checkout['with'])
        self.assertNotIn('GH_TOKEN', json.dumps(self.workflow))

    def test_no_floating_actions_or_semantic_release(self):
        for step in self.steps:
            if 'uses' in step:
                self.assertRegex(step['uses'], r'^[\w-]+/[\w-]+@[0-9a-f]{40}$')
                self.assertNotIn('semantic-release', step['uses'])

    def test_build_is_labeled_and_precedes_cloud_credentials(self):
        builds = [(i, step) for i, step in enumerate(self.steps)
                  if 'docker build ' in step.get('run', '')]
        self.assertEqual(len(builds), 1)
        index, step = builds[0]
        self.assertIn('org.opencontainers.image.revision=$IMAGE_TAG', step['run'])
        self.assertIn('pip check', step['run'])
        first_aws = next(i for i, step in enumerate(self.steps)
                         if step.get('uses', '').startswith('aws-actions/'))
        self.assertLess(index, first_aws)
        self.assertEqual(self.workflow['env']['IMAGE_TAG'], '${{ github.sha }}')

    def test_all_existing_registry_destinations_are_preserved(self):
        credentials = [step for step in self.steps
                       if step.get('uses', '').startswith(
                           'aws-actions/configure-aws-credentials@')]
        self.assertEqual(len(credentials), 3)
        for step, suffix, region in zip(credentials, ('', '_2', '_3'),
                                        ('eu-west-1', 'ap-southeast-1', 'eu-west-1')):
            self.assertEqual(step['with']['aws-access-key-id'],
                             '${{ secrets.AWS_ACCESS_KEY_ID%s }}' % suffix)
            self.assertEqual(step['with']['aws-secret-access-key'],
                             '${{ secrets.AWS_SECRET_ACCESS_KEY%s }}' % suffix)
            self.assertEqual(step['with']['aws-region'], region)
        publishes = [step for step in self.steps if step.get('run') ==
                     'bash .github/scripts/publish-image.sh']
        self.assertEqual(len(publishes), 3)
        for step, login in zip(publishes, ('login-ecr', 'login-ecr-2', 'login-ecr-3')):
            self.assertEqual(step['env']['ECR_REGISTRY'],
                             '${{ steps.%s.outputs.registry }}' % login)
            self.assertNotIn('continue-on-error', step)

    def test_publication_is_serial_and_bounded(self):
        self.assertEqual(self.workflow.get('concurrency'), {
            'group': 'api-image-publication', 'cancel-in-progress': 'false',
        })
        self.assertLessEqual(int(self.job['timeout-minutes']), 30)


FAKE_DOCKER = '''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
log = Path(os.environ['DOCKER_CALLS'])
calls = json.loads(log.read_text()) if log.exists() else []
calls.append(sys.argv[1:])
log.write_text(json.dumps(calls))
if len(calls) == int(os.environ.get('FAIL_AT', '0')):
    sys.exit(42)
if sys.argv[1:3] == ['image', 'inspect']:
    if '.Config.Labels' in sys.argv[4]:
        print(os.environ['MOCK_REVISION'])
    else:
        print('another.invalid/api@sha256:' + 'c' * 64)
        print(os.environ['MOCK_DIGEST'])
'''


class PublicationScriptTests(unittest.TestCase):
    def run_publication(self, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            docker = temporary / 'docker'
            docker.write_text(FAKE_DOCKER)
            docker.chmod(0o700)
            log = temporary / 'calls.json'
            summary = temporary / 'summary.md'
            env = dict(os.environ, PATH=directory + os.pathsep + os.environ['PATH'],
                       DOCKER_CALLS=str(log), GITHUB_STEP_SUMMARY=str(summary),
                       ECR_REGISTRY='example.invalid', ECR_REPOSITORY='api',
                       IMAGE_TAG=SHA, BUILD_NAME='candidate', MOCK_REVISION=SHA,
                       MOCK_DIGEST=IMAGE + '@' + DIGEST)
            env.update(overrides)
            result = subprocess.run(
                ['bash', '.github/scripts/publish-image.sh'], cwd=str(ROOT),
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=10,
            )
            return (result, json.loads(log.read_text()) if log.exists() else [],
                    summary.read_text() if summary.exists() else '')

    def test_publish_exact_sha_then_latest_with_digest_receipt(self):
        result, calls, summary = self.run_publication()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call for call in calls if call[0] == 'push'], [
            ['push', IMAGE + ':' + SHA], ['push', IMAGE + ':latest'],
        ])
        self.assertEqual([call for call in calls if call[0] == 'tag'], [
            ['tag', 'candidate', IMAGE + ':' + SHA],
            ['tag', 'candidate', IMAGE + ':latest'],
        ])
        self.assertIn(IMAGE + '@' + DIGEST, summary)
        self.assertIn(SHA, summary)
        self.assertIn('latest', summary)

    def test_wrong_revision_is_rejected_before_tag_or_push(self):
        result, calls, summary = self.run_publication(MOCK_REVISION='old')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary, '')

    def test_invalid_sha_is_rejected_before_docker(self):
        for value in ('', 'latest', 'a' * 39, 'x' * 40):
            with self.subTest(value=value):
                result, calls, _ = self.run_publication(IMAGE_TAG=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_each_docker_failure_stops_remaining_commands(self):
        for failure in range(1, 7):
            with self.subTest(failure=failure):
                result, calls, summary = self.run_publication(FAIL_AT=str(failure))
                self.assertEqual(result.returncode, 42, result.stderr)
                self.assertEqual(len(calls), failure)
                if failure <= 4:
                    self.assertEqual(summary, '')
                else:
                    self.assertIn(IMAGE + '@' + DIGEST, summary)
                    self.assertNotIn('latest', summary)

    def test_missing_or_invalid_matching_digest_does_not_publish_latest(self):
        for value in ('', IMAGE + '@sha256:bad', 'other.invalid/api@' + DIGEST):
            with self.subTest(value=value):
                result, calls, summary = self.run_publication(MOCK_DIGEST=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([call for call in calls if call[0] == 'push'],
                                 [['push', IMAGE + ':' + SHA]])
                self.assertEqual(summary, '')


if __name__ == '__main__':
    unittest.main()
