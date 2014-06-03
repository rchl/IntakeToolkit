#!/usr/bin/python3
# Copyright (C) 2014 Opera Software ASA. All rights reserved.
#
# This file is an original work developed by Opera Software ASA.

import json
import os.path
import re
import subprocess
import sys
import tempfile
import threading
import urllib
from functools import partial

import sublime
import sublime_plugin

API_HOST = 'https://browser-resources.oslo.osa/'
API_LATEST_LIST_URL = API_HOST + 'api/iwilldo/combined/latest'
API_UPDATE_ITEM_URL = API_HOST + 'api/iwilldo/items/%s/'
API_USER_TOKEN_URL = API_HOST + 'repatch/api/obtain-token?format=json'
# Interval (in ms) at which the list is updated when the buffer is active.
CHECK_INTERVAL = 15000
# Pref name constants.
PREF_NAME_USERNAME = 'will_do_list_username'
PREF_NAME_REPOROOT = 'will_do_list_repo_root'
PREF_NAME_AUTHTOKEN = 'will_do_list_auth_token'
PACKAGE_PATH = 'Packages/IntakeToolkit'
# Message shown when the plugin is not configured.
FIRST_USE_MESSAGE = '''
Some preferences must be set before you can use the Intake Toolkit plugin.

In Preferences -> "Settings - User" add:

  "%s": "replace_with_your_username",
  "%s": "replace_with_your_token",

Get your token from:
  %s

In Project -> "Edit Project" add:

  "settings":
  {
    "%s": "insert_path_to_the_repository_root",
  }
''' % (PREF_NAME_USERNAME, PREF_NAME_AUTHTOKEN, API_USER_TOKEN_URL,
       PREF_NAME_REPOROOT)


def normalize_path(path):
  """Normalizes path to Unix format, converting back to forward slashes."""
  return path.strip().replace('\\', '/')


def run_process(command, working_dir, dont_block=False):
  """Wrapper around subprocess that hides console window on Windows."""
  startupinfo = None
  if sublime.platform() == 'windows':
    # Don't let console window pop-up on Windows.
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
  process = subprocess.Popen(command,
                             cwd=working_dir,
                             stdin=subprocess.PIPE,
                             stdout=(None if dont_block else subprocess.PIPE),
                             stderr=(None if dont_block else subprocess.PIPE),
                             startupinfo=startupinfo)
  # No output when not blocking.
  if dont_block:
    return

  output, error = process.communicate()
  return (str(output, "utf-8") if output else None, process.returncode)


class WillDoListShowCommand(sublime_plugin.TextCommand):
  def run(self, edit):
    view = self.view
    if (not view.settings().has(PREF_NAME_USERNAME) or not
            view.settings().has(PREF_NAME_REPOROOT) or not
            view.settings().has(PREF_NAME_AUTHTOKEN)):
      new_view = self.view.window().new_file()
      new_view.set_scratch(True)
      new_view.insert(edit, 0, FIRST_USE_MESSAGE)
      return

    # Focus view if already created.
    if iwilldolist.get_view():
      self.view.window().focus_view(iwilldolist.get_view())
      return

    new_view = self.view.window().new_file()
    new_view.set_syntax_file(
        '%s/syntax/Intake Toolkit.tmLanguage' % PACKAGE_PATH)
    new_view.insert(edit, 0, 'Loading...')
    new_view.set_read_only(True)
    new_view.run_command('will_do_list_start_update_interval')
    new_view.settings().set('line_numbers', False)
    new_view.settings().set('rulers', [])
    new_view.settings().set('draw_white_space', 'none')
    new_view.settings().set('draw_indent_guides', False)


class WillDoListStartUpdateIntervalCommand(sublime_plugin.TextCommand):
  def run(self, edit):
    iwilldolist.initialize(self.view)
    iwilldolist.trigger_update()


class WillDoListUpdateWithDataCommand(sublime_plugin.TextCommand):
  """Updates buffer with the data fetched from the network."""

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self._output = []
    self._current_line = 0

  def _reset_line_data(self):
    self._current_line = 0
    self._output = []

  def _add_line(self, text):
    self._output.append(text)
    # +1 as the lines will be joined later.
    self._current_line += text.count('\n') + 1

  def _get_output(self):
    return '\n'.join(self._output)

  def run(self, edit, data):
    view = self.view
    self._reset_line_data()
    line_to_item_mapping = {}
    initial_cursor_pos = 0
    if 'error' in data:
      self._add_line(data['error'])
    else:
      name = '%s: %s' % (data['bts_issue'], data['title'])
      view.set_name(name)
      self._add_line(name)
      self._add_line('Clean upstream: %s' % data['base_commit'])
      self._add_line('')
      self._add_line('Keyboard shortcuts:')
      self._add_line('  c - claim/unclaim the file(s)')
      self._add_line('  m - run merge tool on the file(s)')
      self._add_line('  d - show diff of the upstream changes')
      self._add_line('  u - update last-modified SHA of the file(s)')
      self._add_line('  o - open the file(s) (alt+o to open upstream version)')
      self._add_line('')
      initial_cursor_pos = len(self._get_output())
      for group in data['groups']:
        self._add_line('%s' % group['title'])
        for item in group['items']:
          line_to_item_mapping[self._current_line] = item
          self._add_line('  %s [%s] %s' % ('√' if item['closed'] else ' ',
                                           item['claimed_by'],
                                           item['name']))
        self._add_line('')
      iwilldolist.set_line_to_item_mapping(line_to_item_mapping)
      iwilldolist.set_upstream_sha(data['base_commit'])

    view.set_read_only(False)
    view.replace(edit, sublime.Region(0, view.size()), self._get_output())
    view.set_read_only(True)

    if not view.is_scratch():
      view.set_scratch(True)
      # On first load, set cursor to the beginning.
      selection = view.sel()
      selection.clear()
      selection.add(sublime.Region(initial_cursor_pos, initial_cursor_pos))

    # Highlight current user's mail.
    regions = view.find_all('\\b%s@opera.com' % iwilldolist.get_username())
    view.add_regions('username_regions',
                     regions,
                     scope='whatever',
                     flags=sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE |
                         sublime.DRAW_SOLID_UNDERLINE)

    # Add gutter icons based on last-synchronized tag of the item.
    regions_processed = []
    regions_unprocessed = []
    regions_invalid = []
    lines_regions = view.lines(sublime.Region(0, view.size()))
    for line in line_to_item_mapping:
      copied = iwilldolist.get_copied_info_for_item(line_to_item_mapping[line])
      if not copied:
        regions_invalid.append(lines_regions[line])
      elif copied['last_synchronized'] == iwilldolist.get_upstream_sha():
        regions_processed.append(lines_regions[line])
      else:
        regions_unprocessed.append(lines_regions[line])
    # TODO(rchlodnicki): There is a bug with rendering gutter icons so this
    # either doesn't work or works randomly.
    # http://www.sublimetext.com/forum/viewtopic.php?f=2&t=16214
    view.add_regions('files_processed', regions_processed,
                     scope='markup.inserted', icon='circle',
                     flags=sublime.HIDDEN)
    view.add_regions('files_unprocessed', regions_unprocessed,
                     scope='comment', icon='circle',
                     flags=sublime.HIDDEN)
    view.add_regions('files_invalid', regions_invalid,
                     scope='markup.deleted', icon='circle',
                     flags=sublime.HIDDEN)


class WillDoListItemToggleClaimCommand(sublime_plugin.TextCommand):
  def _toggle_username_in(self, text):
    current_user = iwilldolist.get_username()
    users = [] if not text else text.split(' ')
    if current_user in users:
      users.remove(current_user)
    else:
      users.append(current_user)
    return ' '.join(users)

  def _on_claim_updated(self, data):
    iwilldolist.trigger_update()

  def run(self, edit):
    for item in iwilldolist.get_items_for_selection(self.view):
      new_claimed_by = self._toggle_username_in(item['claimed_by'])
      NetworkWorkerThread(
          API_UPDATE_ITEM_URL % item['id'],
          'PATCH',
          bytearray('{"claimed_by": "%s"}' % new_claimed_by, 'utf-8'),
          self._on_claim_updated).start()


class WillDoListItemOpenCommand(sublime_plugin.TextCommand):
  def run(self, edit):
    view = self.view
    for item in iwilldolist.get_items_for_selection(view):
      view.window().open_file(
          os.path.join(iwilldolist.get_reporoot(), item['name']))


class WillDoListItemOpenUpstreamCommand(sublime_plugin.TextCommand):
  def run(self, edit):
    view = self.view
    for item in iwilldolist.get_items_for_selection(view):
      info = iwilldolist.get_copied_info_for_item(item)
      view.window().open_file(
          os.path.join(iwilldolist.get_reporoot(), info['copied_from_path']))


class WillDoListItemMergeCommand(sublime_plugin.TextCommand):
  def run(self, edit):
    view = self.view
    for item in iwilldolist.get_items_for_selection(view):
      copied_info = iwilldolist.get_copied_info_for_item(item)
      if copied_info:
        command = [
            'python',  # not sys.executable due to discrepancy between Sublime's
                       # built-in python and system installed one.
            'chromium_intake.py',
            '--end-commit', iwilldolist.get_upstream_sha(),
            '--dest', os.path.join(iwilldolist.get_reporoot(), item['name']),
            '--mergetool=p4merge',
            '--tempdir', tempfile.gettempdir()
        ]
        run_process(
            command,
            os.path.join(iwilldolist.get_reporoot(), 'desktop', 'tools'),
            dont_block=True)


class WillDoListItemDiffCommand(sublime_plugin.TextCommand):
  def run(self, edit):
    view = self.view
    for item in iwilldolist.get_items_for_selection(view):
      copied_info = iwilldolist.get_copied_info_for_item(item)
      if copied_info:
        # TODO(rchlodnicki): This is a bit messy. EmbeddedInfo returns
        # copied_from_path relative to the desktop repo while ExternalInfo
        # returns absolute path. Removing 'chromium/src/' part from the
        # EmbeddedInfo should make it work. Absolute path is fine.
        # To make things worse, on Mac the path is absolute...
        copied_from_path = normalize_path(copied_info['copied_from_path'])
        copied_from_path = copied_from_path.replace(
            iwilldolist.get_reporoot() + '/', '').replace('chromium/src/', '')
        command = ['git',
                   'diff',
                   '%s..%s' % (copied_info['last_synchronized'],
                               iwilldolist.get_upstream_sha()),
                   '--exit-code',
                   '--',
                   copied_from_path]
        chromium_src = normalize_path(
            os.path.join(iwilldolist.get_reporoot(), 'chromium', 'src'))
        (output, returncode) = run_process(command, chromium_src)
        if returncode != 1:
          output = 'No changes'
        new_view = view.window().new_file()
        new_view.set_name(' '.join(command))
        new_view.run_command("write_git_diff_to_view", {"content": output})


class WillDoListItemUpdateShaCommand(sublime_plugin.TextCommand):
  def run(self, edit):
    view = self.view
    for item in iwilldolist.get_items_for_selection(view):
      copied_info = iwilldolist.get_copied_info_for_item(item)
      if copied_info:
        copied_info.set_last_sync(iwilldolist.get_upstream_sha())
    iwilldolist.trigger_update()


class WriteGitDiffToViewCommand(sublime_plugin.TextCommand):
  def run(self, edit, content):
    view = self.view
    view.insert(edit, 0, content)
    view.set_scratch(True)
    view.set_read_only(True)
    view.set_syntax_file(
        "%s/syntax/Git Commit View.tmLanguage" % PACKAGE_PATH)
    selection = view.sel()
    selection.clear()
    selection.add(sublime.Region(0, 0))


class EventObserver(sublime_plugin.EventListener):
  def on_pre_close(self, view):
    iwilldolist.on_view_closing(view)


class NetworkWorkerThread(threading.Thread):
  def __init__(self, url, method, data, callback):
    super(NetworkWorkerThread, self).__init__()
    self._url = url
    self._method = method
    self._data = data
    self._callback = callback

  def _fetch_url(self, url):
    try:
      data = urllib.request.urlopen(url).read().decode('utf-8')
    except urllib.error.URLError as ex:
      data = '{"error": "Failed retrieving data. %s"}' % ex.reason
    return data

  def run(self):
    headers = {
        'Content-Type': 'application/json',
        'Authorization': 'Token %s' % iwilldolist.get_auth_token()
    }
    request = urllib.request.Request(self._url,
                                     data=self._data,
                                     headers=headers,
                                     method=self._method)
    data = json.loads(self._fetch_url(request))
    sublime.set_timeout(partial(self._callback, data))


class IWillDoList(object):
  """Global class that controls the plugin."""

  def __init__(self):
    # The View that is currently showing the IWillDo list. Only one such view
    # can exist at a time.
    self._view = None
    # Mapping from the line number in generated IWillDo list to an item object.
    self._line_to_item_mapping = {}
    self._username = ''
    self._auth_token = ''
    self._reporoot = ''
    self._upstream_sha = ''
    self._initialized = False

  def initialize(self, view):
    self._view = view
    self._username = view.settings().get(PREF_NAME_USERNAME)
    self._reporoot = view.settings().get(PREF_NAME_REPOROOT)
    self._auth_token = view.settings().get(PREF_NAME_AUTHTOKEN)
    if not self._initialized:
      # Import libintake package from the desktop tools dir. We have to do it
      # only once. We must also make the imported module be visible in the
      # global scope.
      sys.path.append(os.path.join(view.settings().get(PREF_NAME_REPOROOT),
                                   'desktop', 'tools', 'libintake'))
      global CopiedFile
      from copied_file import CopiedFile

    self._initialized = True

  def get_view(self):
    return self._view

  def get_username(self):
    return self._username

  def get_reporoot(self):
    return self._reporoot

  def get_auth_token(self):
    return self._auth_token

  def get_copied_info_for_item(self, item):
    absolute_path = os.path.join(self._reporoot, item['name'])
    if os.path.exists(absolute_path):
      return CopiedFile.create(
          normalize_path(absolute_path),
          normalize_path(os.path.join(self._reporoot, 'chromium', 'src')),
          allow_caching=False)
    return None

  def set_line_to_item_mapping(self, mapping):
    self._line_to_item_mapping = mapping

  def get_upstream_sha(self):
    return self._upstream_sha

  def set_upstream_sha(self, sha):
    self._upstream_sha = sha

  def get_items_for_selection(self, view):
    items = []
    for region in view.sel():
      lines = []
      for line_region in view.lines(region):
        line_nr = view.rowcol(line_region.a)[0]
        if line_nr not in lines:
          if line_nr in self._line_to_item_mapping:
            items.append(self._line_to_item_mapping[line_nr])
    return items

  def on_view_closing(self, view):
    if self._view and self._view.id() == view.id():
      self._view = None

  def trigger_update(self):
    """Triggers update of the list on the network thread."""
    if self._view:
      NetworkWorkerThread(API_LATEST_LIST_URL,
                          'GET',
                          None,
                          self._on_data_fetched).start()

  def _on_data_fetched(self, data):
    if self._view:
      self._view.run_command('will_do_list_update_with_data', {'data': data})
      sublime.set_timeout(self.trigger_update, CHECK_INTERVAL)


iwilldolist = IWillDoList()
