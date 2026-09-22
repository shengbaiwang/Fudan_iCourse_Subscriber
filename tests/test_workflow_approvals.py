from copy import deepcopy
from datetime import datetime
import unittest
from unittest.mock import Mock, patch

from local_web.github_client import GitHubAPIError, GitHubClient
from local_web.workflow_approvals import WorkflowApprovals, eligible, reconcile

NOW = datetime.fromisoformat('2026-09-22T12:00:00+00:00').timestamp()
RUN = dict(id=42, status='completed', conclusion='action_required', run_attempt=1,
           event='workflow_dispatch', head_branch='main', head_sha='trusted',
           path='.github/workflows/single_run.yml', pull_requests=[],
           repository={'full_name': 'owner/repo'}, head_repository={'full_name': 'owner/repo'},
           actor={'login': 'owner'}, triggering_actor={'login': 'owner'},
           created_at='2026-09-22T11:59:00Z')


class ApprovalsTest(unittest.TestCase):
    def client(self, fresh=None, user='owner', head='trusted'):
        client = GitHubClient('owner', 'repo', 'fixture-token')
        responses = {
            '/repos/owner/repo/actions/runs?branch=main&per_page=100': {'workflow_runs': [deepcopy(RUN)]},
            '/user': {'login': user},
            '/repos/owner/repo/actions/runs/42': deepcopy(RUN if fresh is None else fresh),
            '/repos/owner/repo/git/ref/heads/main': {'object': {'sha': head}},
        }
        client._json = Mock(side_effect=lambda path: responses[path])
        client._request = Mock(return_value=b'')
        return client

    def test_approves_only_eligible_first_attempt_using_approval_not_rerun(self):
        client = self.client()
        self.assertEqual(reconcile(client, now=NOW), {'approved': [42], 'errors': []})
        client._request.assert_called_once_with('/repos/owner/repo/actions/runs/42/approve', method='POST')

    def test_rejects_untrusted_old_or_already_executed_runs(self):
        cases = [dict(event='pull_request'), dict(event='push'), dict(head_branch='dev'),
                 dict(actor={'login': 'other'}), dict(triggering_actor={'login': 'other'}),
                 dict(repository={'full_name': 'owner/other'}), dict(head_repository=None),
                 dict(head_repository={'full_name': 'outsider/repo'}), dict(pull_requests=[{'number': 1}]),
                 dict(pull_requests=None), dict(path='.github/workflows/delete_course.yml'),
                 dict(path='.github/workflows/deploy-frontend.yml'), dict(status='waiting'),
                 dict(conclusion='failure'), dict(conclusion='success'), dict(run_attempt=2),
                 dict(created_at='2026-09-20T00:00:00Z'), dict(created_at='2026-09-23T00:00:00Z'),
                 dict(created_at='invalid'), dict(id='../other')]
        for change in cases:
            with self.subTest(change=change):
                self.assertFalse(eligible({**RUN, **change}, 'owner', 'repo', NOW))
                client = self.client(fresh={**RUN, **change})
                reconcile(client, now=NOW)
                client._request.assert_not_called()

    def test_schedule_and_other_workflows_are_never_approved(self):
        for change in [{'event': 'schedule'}, {'path': '.github/workflows/check.yml'},
                       {'path': '.github/workflows/talk_transcribe.yml'}]:
            self.assertFalse(eligible({**RUN, **change}, 'owner', 'repo', NOW))
            client = self.client(fresh={**RUN, **change})
            reconcile(client, now=NOW)
            client._request.assert_not_called()

    def test_changed_main_non_owner_token_or_logout_prevents_approval(self):
        for client, allowed in [(self.client(head='new'), True), (self.client(user='collaborator'), True),
                                (self.client(), False)]:
            reconcile(client, can_approve=lambda: allowed, now=NOW)
            client._request.assert_not_called()

    def test_github_denial_is_visible_and_never_retried_as_rerun(self):
        client = self.client()
        client._request.side_effect = GitHubAPIError(403, 'not permitted')
        result = reconcile(client, now=NOW)
        self.assertEqual(result['approved'], [])
        self.assertIn('403', result['errors'][0]['message'])
        self.assertEqual(client._request.call_count, 1)

    def test_service_throttles_and_resets_after_logout(self):
        identity = Mock(return_value='session')
        service = WorkflowApprovals(Mock(), identity)
        with patch('local_web.workflow_approvals.reconcile', return_value={'approved': [], 'errors': [{'message': '403'}]}) as check:
            service.check()
            service.check()
            self.assertEqual(check.call_count, 1)
            identity.return_value = None
            self.assertEqual(service.check(), {'approved': [], 'errors': []})
            identity.return_value = 'new-session'
            service.check()
            self.assertEqual(check.call_count, 2)


if __name__ == '__main__':
    unittest.main()
