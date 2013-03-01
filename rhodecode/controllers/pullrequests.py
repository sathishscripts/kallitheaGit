# -*- coding: utf-8 -*-
"""
    rhodecode.controllers.pullrequests
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    pull requests controller for rhodecode for initializing pull requests

    :created_on: May 7, 2012
    :author: marcink
    :copyright: (C) 2010-2012 Marcin Kuzminski <marcin@python-works.com>
    :license: GPLv3, see COPYING for more details.
"""
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import logging
import traceback
import formencode

from webob.exc import HTTPNotFound, HTTPForbidden
from collections import defaultdict
from itertools import groupby

from pylons import request, response, session, tmpl_context as c, url
from pylons.controllers.util import abort, redirect
from pylons.i18n.translation import _

from rhodecode.lib.compat import json
from rhodecode.lib.base import BaseRepoController, render
from rhodecode.lib.auth import LoginRequired, HasRepoPermissionAnyDecorator,\
    NotAnonymous
from rhodecode.lib import helpers as h
from rhodecode.lib import diffs
from rhodecode.lib.utils import action_logger, jsonify
from rhodecode.lib.vcs.exceptions import EmptyRepositoryError
from rhodecode.lib.vcs.backends.base import EmptyChangeset
from rhodecode.lib.diffs import LimitedDiffContainer
from rhodecode.model.db import User, PullRequest, ChangesetStatus,\
    ChangesetComment
from rhodecode.model.pull_request import PullRequestModel
from rhodecode.model.meta import Session
from rhodecode.model.repo import RepoModel
from rhodecode.model.comment import ChangesetCommentsModel
from rhodecode.model.changeset_status import ChangesetStatusModel
from rhodecode.model.forms import PullRequestForm

log = logging.getLogger(__name__)


class PullrequestsController(BaseRepoController):

    @LoginRequired()
    @HasRepoPermissionAnyDecorator('repository.read', 'repository.write',
                                   'repository.admin')
    def __before__(self):
        super(PullrequestsController, self).__before__()
        repo_model = RepoModel()
        c.users_array = repo_model.get_users_js()
        c.users_groups_array = repo_model.get_users_groups_js()

    def _get_repo_refs(self, repo, rev=None):
        """return a structure with repo's interesting changesets, suitable for
        the selectors in pullrequest.html"""
        branches = [('branch:%s:%s' % (k, v), k)
                    for k, v in repo.branches.iteritems()]
        bookmarks = [('book:%s:%s' % (k, v), k)
                     for k, v in repo.bookmarks.iteritems()]
        tags = [('tag:%s:%s' % (k, v), k)
                for k, v in repo.tags.iteritems()
                if k != 'tip']

        tip = repo.tags['tip']
        colontip = ':' + tip
        tips = [x[1] for x in branches + bookmarks + tags
                if x[0].endswith(colontip)]
        selected = 'tag:tip:%s' % tip
        special = [(selected, 'tip (%s)' % ', '.join(tips))]

        if rev:
            selected = 'rev:%s:%s' % (rev, rev)
            special.append((selected, rev))

        return [(special, _("Special")),
                (bookmarks, _("Bookmarks")),
                (branches, _("Branches")),
                (tags, _("Tags")),
                ], selected

    def _get_is_allowed_change_status(self, pull_request):
        owner = self.rhodecode_user.user_id == pull_request.user_id
        reviewer = self.rhodecode_user.user_id in [x.user_id for x in
                                                   pull_request.reviewers]
        return (self.rhodecode_user.admin or owner or reviewer)

    def show_all(self, repo_name):
        c.pull_requests = PullRequestModel().get_all(repo_name)
        c.repo_name = repo_name
        return render('/pullrequests/pullrequest_show_all.html')

    @NotAnonymous()
    def index(self):
        org_repo = c.rhodecode_db_repo

        if org_repo.scm_instance.alias != 'hg':
            log.error('Review not available for GIT REPOS')
            raise HTTPNotFound

        try:
            org_repo.scm_instance.get_changeset()
        except EmptyRepositoryError, e:
            h.flash(h.literal(_('There are no changesets yet')),
                    category='warning')
            redirect(url('summary_home', repo_name=org_repo.repo_name))

        org_rev = request.GET.get('rev_end')
        # rev_start is not directly useful - its parent could however be used
        # as default for other and thus give a simple compare view
        #other_rev = request.POST.get('rev_start')

        other_repos_info = {}

        c.org_repos = []
        c.org_repos.append((org_repo.repo_name, org_repo.repo_name))
        c.default_org_repo = org_repo.repo_name
        c.org_refs, c.default_org_ref = self._get_repo_refs(org_repo.scm_instance, org_rev)

        c.other_repos = []
        # add org repo to other so we can open pull request against itself
        c.other_repos.extend(c.org_repos)
        c.default_other_repo = org_repo.repo_name
        c.default_other_refs, c.default_other_ref = self._get_repo_refs(org_repo.scm_instance)
        usr_data = lambda usr: dict(user_id=usr.user_id,
                                    username=usr.username,
                                    firstname=usr.firstname,
                                    lastname=usr.lastname,
                                    gravatar_link=h.gravatar_url(usr.email, 14))
        other_repos_info[org_repo.repo_name] = {
            'user': usr_data(org_repo.user),
            'description': org_repo.description,
            'revs': h.select('other_ref', c.default_other_ref,
                             c.default_other_refs, class_='refs')
        }

        # gather forks and add to this list ... even though it is rare to
        # request forks to pull their parent
        for fork in org_repo.forks:
            c.other_repos.append((fork.repo_name, fork.repo_name))
            refs, default_ref = self._get_repo_refs(fork.scm_instance)
            other_repos_info[fork.repo_name] = {
                'user': usr_data(fork.user),
                'description': fork.description,
                'revs': h.select('other_ref', default_ref, refs, class_='refs')
            }

        # add parents of this fork also, but only if it's not empty
        if org_repo.parent and org_repo.parent.scm_instance.revisions:
            c.default_other_repo = org_repo.parent.repo_name
            c.default_other_refs, c.default_other_ref = self._get_repo_refs(org_repo.parent.scm_instance)
            c.other_repos.append((org_repo.parent.repo_name, org_repo.parent.repo_name))
            other_repos_info[org_repo.parent.repo_name] = {
                'user': usr_data(org_repo.parent.user),
                'description': org_repo.parent.description,
                'revs': h.select('other_ref', c.default_other_ref,
                                 c.default_other_refs, class_='refs')
            }

        c.other_repos_info = json.dumps(other_repos_info)
        # other repo owner
        c.review_members = []
        return render('/pullrequests/pullrequest.html')

    @NotAnonymous()
    def create(self, repo_name):
        repo = RepoModel()._get_repo(repo_name)
        try:
            _form = PullRequestForm(repo.repo_id)().to_python(request.POST)
        except formencode.Invalid, errors:
            log.error(traceback.format_exc())
            if errors.error_dict.get('revisions'):
                msg = 'Revisions: %s' % errors.error_dict['revisions']
            elif errors.error_dict.get('pullrequest_title'):
                msg = _('Pull request requires a title with min. 3 chars')
            else:
                msg = _('error during creation of pull request')

            h.flash(msg, 'error')
            return redirect(url('pullrequest_home', repo_name=repo_name))

        org_repo = _form['org_repo']
        org_ref = _form['org_ref']
        other_repo = _form['other_repo']
        other_ref = _form['other_ref']
        revisions = _form['revisions']
        reviewers = _form['review_members']

        title = _form['pullrequest_title']
        description = _form['pullrequest_desc']

        try:
            pull_request = PullRequestModel().create(
                self.rhodecode_user.user_id, org_repo, org_ref, other_repo,
                other_ref, revisions, reviewers, title, description
            )
            Session().commit()
            h.flash(_('Successfully opened new pull request'),
                    category='success')
        except Exception:
            h.flash(_('Error occurred during sending pull request'),
                    category='error')
            log.error(traceback.format_exc())
            return redirect(url('pullrequest_home', repo_name=repo_name))

        return redirect(url('pullrequest_show', repo_name=other_repo,
                            pull_request_id=pull_request.pull_request_id))

    @NotAnonymous()
    @jsonify
    def update(self, repo_name, pull_request_id):
        pull_request = PullRequest.get_or_404(pull_request_id)
        if pull_request.is_closed():
            raise HTTPForbidden()
        #only owner or admin can update it
        owner = pull_request.author.user_id == c.rhodecode_user.user_id
        if h.HasPermissionAny('hg.admin', 'repository.admin')() or owner:
            reviewers_ids = map(int, filter(lambda v: v not in [None, ''],
                       request.POST.get('reviewers_ids', '').split(',')))

            PullRequestModel().update_reviewers(pull_request_id, reviewers_ids)
            Session().commit()
            return True
        raise HTTPForbidden()

    @NotAnonymous()
    @jsonify
    def delete(self, repo_name, pull_request_id):
        pull_request = PullRequest.get_or_404(pull_request_id)
        #only owner can delete it !
        if pull_request.author.user_id == c.rhodecode_user.user_id:
            PullRequestModel().delete(pull_request)
            Session().commit()
            h.flash(_('Successfully deleted pull request'),
                    category='success')
            return redirect(url('admin_settings_my_account', anchor='pullrequests'))
        raise HTTPForbidden()

    def _load_compare_data(self, pull_request, enable_comments=True):
        """
        Load context data needed for generating compare diff

        :param pull_request:
        :type pull_request:
        """
        org_repo = pull_request.org_repo
        (org_ref_type,
         org_ref_name,
         org_ref_rev) = pull_request.org_ref.split(':')

        other_repo = org_repo
        (other_ref_type,
         other_ref_name,
         other_ref_rev) = pull_request.other_ref.split(':')

        # despite opening revisions for bookmarks/branches/tags, we always
        # convert this to rev to prevent changes after bookmark or branch change
        org_ref = ('rev', org_ref_rev)
        other_ref = ('rev', other_ref_rev)

        c.org_repo = org_repo
        c.other_repo = other_repo

        c.fulldiff = fulldiff = request.GET.get('fulldiff')

        c.cs_ranges = [org_repo.get_changeset(x) for x in pull_request.revisions]

        other_ref = ('rev', getattr(c.cs_ranges[0].parents[0]
                                  if c.cs_ranges[0].parents
                                  else EmptyChangeset(), 'raw_id'))

        c.statuses = org_repo.statuses([x.raw_id for x in c.cs_ranges])

        c.org_ref = org_ref[1]
        c.org_ref_type = org_ref[0]
        c.other_ref = other_ref[1]
        c.other_ref_type = other_ref[0]

        diff_limit = self.cut_off_limit if not fulldiff else None

        #we swap org/other ref since we run a simple diff on one repo
        _diff = diffs.differ(org_repo, other_ref, other_repo, org_ref)

        diff_processor = diffs.DiffProcessor(_diff or '', format='gitdiff',
                                             diff_limit=diff_limit)
        _parsed = diff_processor.prepare()

        c.limited_diff = False
        if isinstance(_parsed, LimitedDiffContainer):
            c.limited_diff = True

        c.files = []
        c.changes = {}
        c.lines_added = 0
        c.lines_deleted = 0
        for f in _parsed:
            st = f['stats']
            if st[0] != 'b':
                c.lines_added += st[0]
                c.lines_deleted += st[1]
            fid = h.FID('', f['filename'])
            c.files.append([fid, f['operation'], f['filename'], f['stats']])
            diff = diff_processor.as_html(enable_comments=enable_comments,
                                          parsed_lines=[f])
            c.changes[fid] = [f['operation'], f['filename'], diff]

    def show(self, repo_name, pull_request_id):
        repo_model = RepoModel()
        c.users_array = repo_model.get_users_js()
        c.users_groups_array = repo_model.get_users_groups_js()
        c.pull_request = PullRequest.get_or_404(pull_request_id)
        c.allowed_to_change_status = self._get_is_allowed_change_status(c.pull_request)
        cc_model = ChangesetCommentsModel()
        cs_model = ChangesetStatusModel()
        _cs_statuses = cs_model.get_statuses(c.pull_request.org_repo,
                                            pull_request=c.pull_request,
                                            with_revisions=True)

        cs_statuses = defaultdict(list)
        for st in _cs_statuses:
            cs_statuses[st.author.username] += [st]

        c.pull_request_reviewers = []
        c.pull_request_pending_reviewers = []
        for o in c.pull_request.reviewers:
            st = cs_statuses.get(o.user.username, None)
            if st:
                sorter = lambda k: k.version
                st = [(x, list(y)[0])
                      for x, y in (groupby(sorted(st, key=sorter), sorter))]
            else:
                c.pull_request_pending_reviewers.append(o.user)
            c.pull_request_reviewers.append([o.user, st])

        # pull_requests repo_name we opened it against
        # ie. other_repo must match
        if repo_name != c.pull_request.other_repo.repo_name:
            raise HTTPNotFound

        # load compare data into template context
        enable_comments = not c.pull_request.is_closed()
        self._load_compare_data(c.pull_request, enable_comments=enable_comments)

        # inline comments
        c.inline_cnt = 0
        c.inline_comments = cc_model.get_inline_comments(
                                c.rhodecode_db_repo.repo_id,
                                pull_request=pull_request_id)
        # count inline comments
        for __, lines in c.inline_comments:
            for comments in lines.values():
                c.inline_cnt += len(comments)
        # comments
        c.comments = cc_model.get_comments(c.rhodecode_db_repo.repo_id,
                                           pull_request=pull_request_id)

        try:
            cur_status = c.statuses[c.pull_request.revisions[0]][0]
        except:
            log.error(traceback.format_exc())
            cur_status = 'undefined'
        if c.pull_request.is_closed() and 0:
            c.current_changeset_status = cur_status
        else:
            # changeset(pull-request) status calulation based on reviewers
            c.current_changeset_status = cs_model.calculate_status(
                                            c.pull_request_reviewers,
                                         )
        c.changeset_statuses = ChangesetStatus.STATUSES

        c.as_form = False
        return render('/pullrequests/pullrequest_show.html')

    @NotAnonymous()
    @jsonify
    def comment(self, repo_name, pull_request_id):
        pull_request = PullRequest.get_or_404(pull_request_id)
        if pull_request.is_closed():
            raise HTTPForbidden()

        status = request.POST.get('changeset_status')
        change_status = request.POST.get('change_changeset_status')
        text = request.POST.get('text')
        close_pr = request.POST.get('save_close')

        allowed_to_change_status = self._get_is_allowed_change_status(pull_request)
        if status and change_status and allowed_to_change_status:
            _def = (_('status change -> %s')
                            % ChangesetStatus.get_status_lbl(status))
            if close_pr:
                _def = _('Closing with') + ' ' + _def
            text = text or _def
        comm = ChangesetCommentsModel().create(
            text=text,
            repo=c.rhodecode_db_repo.repo_id,
            user=c.rhodecode_user.user_id,
            pull_request=pull_request_id,
            f_path=request.POST.get('f_path'),
            line_no=request.POST.get('line'),
            status_change=(ChangesetStatus.get_status_lbl(status)
                           if status and change_status
                           and allowed_to_change_status else None),
            closing_pr=close_pr
        )

        action_logger(self.rhodecode_user,
                      'user_commented_pull_request:%s' % pull_request_id,
                      c.rhodecode_db_repo, self.ip_addr, self.sa)

        if allowed_to_change_status:
            # get status if set !
            if status and change_status:
                ChangesetStatusModel().set_status(
                    c.rhodecode_db_repo.repo_id,
                    status,
                    c.rhodecode_user.user_id,
                    comm,
                    pull_request=pull_request_id
                )

            if close_pr:
                if status in ['rejected', 'approved']:
                    PullRequestModel().close_pull_request(pull_request_id)
                    action_logger(self.rhodecode_user,
                              'user_closed_pull_request:%s' % pull_request_id,
                              c.rhodecode_db_repo, self.ip_addr, self.sa)
                else:
                    h.flash(_('Closing pull request on other statuses than '
                              'rejected or approved forbidden'),
                            category='warning')

        Session().commit()

        if not request.environ.get('HTTP_X_PARTIAL_XHR'):
            return redirect(h.url('pullrequest_show', repo_name=repo_name,
                                  pull_request_id=pull_request_id))

        data = {
           'target_id': h.safeid(h.safe_unicode(request.POST.get('f_path'))),
        }
        if comm:
            c.co = comm
            data.update(comm.get_dict())
            data.update({'rendered_text':
                         render('changeset/changeset_comment_block.html')})

        return data

    @NotAnonymous()
    @jsonify
    def delete_comment(self, repo_name, comment_id):
        co = ChangesetComment.get(comment_id)
        if co.pull_request.is_closed():
            #don't allow deleting comments on closed pull request
            raise HTTPForbidden()

        owner = co.author.user_id == c.rhodecode_user.user_id
        if h.HasPermissionAny('hg.admin', 'repository.admin')() or owner:
            ChangesetCommentsModel().delete(comment=co)
            Session().commit()
            return True
        else:
            raise HTTPForbidden()
