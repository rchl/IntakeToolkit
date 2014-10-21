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
from time import gmtime, sleep, strftime

import sublime
import sublime_plugin

API_HOST = 'https://browser-resources.oslo.osa/'
API_LATEST_LIST_URL = API_HOST + 'api/iwilldo/combined/latest'
API_UPDATE_ITEM_URL = API_HOST + 'api/iwilldo/items/%s/'
API_USER_TOKEN_URL = API_HOST + 'api/obtain-token?format=json'
# Interval (in seconds) at which the list is updated when the buffer is active.
CHECK_INTERVAL_SEC = 15
# Pref name constants.
PREF_NAME_USERNAME = 'will_do_list_username'
PREF_NAME_REPOROOT = 'will_do_list_repo_root'
PREF_NAME_AUTHTOKEN = 'will_do_list_auth_token'
PREF_NAME_MERGETOOL = 'will_do_list_merge_tool'
PACKAGE_PATH = 'Packages/IntakeToolkit'
# Supported keyboard shortcuts.
COMMANDS = [
    [
        'c',
        'claim/unclaim the file(s)',
        'will_do_list_item_toggle_claim'
    ],
    [
        'm',
        'run merge tool on the file(s)',
        'will_do_list_item_merge'
    ],
    [
        'd',
        'show diff of the upstream changes',
        'will_do_list_item_diff'
    ],
    [
        'ctrl+d',
        'compare local and upstream files in an external tool',
        'will_do_list_item_compare'
    ],
    [
        'o',
        'open the file',
        'will_do_list_item_open'
    ],
    [
        'alt+o',
        'open upstream file',
        'will_do_list_item_open_upstream'
    ],
    [
        'u',
        'update last-modified SHA of the file(s)',
        'will_do_list_item_update_sha'
    ],
    [
        'l',
        'show git log affecting given file',
        'will_do_list_item_git_log'
    ],
]
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


def get_item_path(item):
  """Returns absolute, normalized path to the given item.

  Args:
      item: The object which contains file item information, including the
      'name' property bearing item's path relative to the repo root.
  """
  return normalize_path(os.path.join(iwilldolist.get_reporoot(),
                                     re.sub(r' \(ERROR\)$', '', item['name'])))


def fixup_upstream_path(path):
  """Modifies the upstream path so that it's relative to the reporoot."""
  # TODO(rchlodnicki): This is a bit messy. EmbeddedInfo returns
  # copied_from_path relative to the desktop repo (even for upstream files)
  # while ExternalInfo returns absolute path. We'll force the path to be
  # absolute so that it always works.
  path = normalize_path(path).replace(iwilldolist.get_reporoot() + '/', '')
  if not path.startswith('chromium/src/'):
    path = 'chromium/src/%s' % path
  return '%s/%s' % (iwilldolist.get_reporoot(), path)


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
    # Not using self.view as it might be a console panel for example.
    view = self.view.window().active_view()
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

    # Possibly there is inactive view somewhere. Revive it.
    for v in view.window().views():
      if v.settings().has('is_will_do_list_view'):
        self.view.window().focus_view(v)
        # Focusing takes care of starting the update interval.
        return

    new_view = self.view.window().new_file()
    new_view.set_syntax_file(
        '%s/syntax/Intake Toolkit.tmLanguage' % PACKAGE_PATH)
    new_view.insert(edit, 0, 'Loading...')
    new_view.set_read_only(True)
    new_view.settings().set('line_numbers', False)
    new_view.settings().set('rulers', [])
    new_view.settings().set('draw_white_space', 'none')
    new_view.settings().set('draw_indent_guides', False)
    new_view.run_command('will_do_list_start_update_interval')


class WillDoListStartUpdateIntervalCommand(sublime_plugin.TextCommand):
  def run(self, edit):
    iwilldolist.initialize(self.view)
    iwilldolist.trigger_update(repeating=True)


class WillDoListUpdateWithDataCommand(sublime_plugin.TextCommand):
  """Updates buffer with the data fetched from the network."""

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self._output = []
    self._current_line = 0
    self._last_viewport_position = (0.0, 0.0)

  def _reset_line_data(self):
    self._current_line = 0
    self._output = []

  def _add_line(self, text):
    self._output.append(text)
    # +1 as the lines will be joined later.
    self._current_line += text.count('\n') + 1

  def _get_output(self):
    return '\n'.join(self._output)

  def _restore_viewport_scroll(self):
    if self.view.viewport_position()[0] > self._last_viewport_position[0]:
      self.view.set_viewport_position(self._last_viewport_position, False)

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
      self._add_line('Last updated: %s' % strftime("%d %b %H:%M:%S", gmtime()))
      self._add_line('')
      self._add_line('Used merge tool: %s (set "%s" pref if you want to change\n'
                     '                 to one of the other supported tools: '
                     'patch, kdiff3, merge, p4merge)' %
                     (iwilldolist.get_mergetool(), PREF_NAME_MERGETOOL))
      self._add_line('')
      self._add_line('Keyboard shortcuts:')
      for command in COMMANDS:
        self._add_line('  %s - %s' % (command[0], command[1]))
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
    self._last_viewport_position = view.viewport_position()
    view.replace(edit, sublime.Region(0, view.size()), self._get_output())
    # Viewport might scroll horizontally after replacing the content. Restore it
    # to the previous position. Need to do it from the timeout as it's not
    # updated synchronously.
    sublime.set_timeout(self._restore_viewport_scroll, 0)
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


class WillDoListUpdateGutterMarksCommand(sublime_plugin.TextCommand):
  """Adds gutter icons based on last-synchronized tag of the item."""

  def run(self, edit):
    view = self.view
    regions_processed = []
    regions_unprocessed = []
    regions_invalid = []
    lines_regions = view.lines(sublime.Region(0, view.size()))
    line_to_item_mapping = iwilldolist.get_line_to_item_mapping()
    for line in line_to_item_mapping:
      copied = iwilldolist.get_copied_info_for_item(line_to_item_mapping[line])
      if not copied:
        regions_invalid.append(lines_regions[line])
      elif copied['last_synchronized'] == iwilldolist.get_upstream_sha():
        regions_processed.append(lines_regions[line])
      else:
        regions_unprocessed.append(lines_regions[line])
    # TODO(rchlodnicki): There is a bug with rendering gutter icons with scope
    # tinting applied so I'm using own graphics right now. Otherwise I could
    # use built-in with scopes like comment, markup.inserted, markup.deleted.
    # http://www.sublimetext.com/forum/viewtopic.php?f=2&t=16214
    view.add_regions('files_processed',
                     regions_processed,
                     scope='whatever',
                     icon='%s/images/circle-green.png' % PACKAGE_PATH,
                     flags=sublime.HIDDEN)
    view.add_regions('files_unprocessed',
                     regions_unprocessed,
                     scope='whatever',
                     icon='%s/images/circle-gray.png' % PACKAGE_PATH,
                     flags=sublime.HIDDEN)
    view.add_regions('files_invalid',
                     regions_invalid,
                     scope='whatever',
                     icon='%s/images/circle-red.png' % PACKAGE_PATH,
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
      iwilldolist.make_request(
          API_UPDATE_ITEM_URL % item['id'],
          'PATCH',
          bytearray('{"claimed_by": "%s"}' % new_claimed_by, 'utf-8'),
          self._on_claim_updated)


class WillDoListItemOpenCommand(sublime_plugin.TextCommand):
  def run(self, edit):
    view = self.view
    for item in iwilldolist.get_items_for_selection(view):
      view.window().open_file(get_item_path(item))


class WillDoListItemOpenUpstreamCommand(sublime_plugin.TextCommand):
  def run(self, edit):
    view = self.view
    for item in iwilldolist.get_items_for_selection(view):
      info = iwilldolist.get_copied_info_for_item(item)
      if info:
        view.window().open_file(fixup_upstream_path(info['copied_from_path']))


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
            '--dest', get_item_path(item),
            '--mergetool=%s' % iwilldolist.get_mergetool(),
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
        command = ['git',
                   'diff',
                   '%s..%s' % (copied_info['last_synchronized'],
                               iwilldolist.get_upstream_sha()),
                   '--exit-code',
                   '--',
                   fixup_upstream_path(copied_info['copied_from_path'])]
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


class WillDoListItemGitLogCommand(sublime_plugin.TextCommand):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self._items_shas = []
    self._file_path = ''

  def on_item_selected(self, index):
    if index == -1:
      return
    new_view = self.view.window().new_file()
    sha = self._items_shas[index]
    command = ['git', 'show', sha, '--exit-code', '--', self._file_path]
    (output, returncode) = run_process(command, iwilldolist.get_reporoot())
    new_view.set_name(' '.join(command))
    new_view.set_scratch(True)
    new_view.run_command("write_git_diff_to_view", {"content": output})

  def run(self, edit):
    self._items_shas = []
    view = self.view
    for item in iwilldolist.get_items_for_selection(view):
      copied_info = iwilldolist.get_copied_info_for_item(item)
      command = ['git',
                 'log',
                 '--pretty=%H;(%ar) %ad;%aE;%s',
                 '--date=local',
                 '--max-count=9000',
                 '--exit-code',
                 '--',
                 get_item_path(item)]
      (output, returncode) = run_process(command, iwilldolist.get_reporoot())
      commands = []
      if returncode == 1:
        self._file_path = get_item_path(item)
        for line in output.split('\n'):
          if line.strip():
            sha, date, author, summary = line.split(';')
            commands.append(['%s <%s>' % (summary, author),
                             '%s %s' % (date, sha)])
            self._items_shas.append(sha)
      else:
        commands.append('No changes')

      view.window().show_quick_panel(commands, self.on_item_selected)
      # Can only handle one item.
      return


class WillDoListItemCompareCommand(sublime_plugin.TextCommand):
  def run(self, edit):
    view = self.view
    for item in iwilldolist.get_items_for_selection(view):
      copied_info = iwilldolist.get_copied_info_for_item(item)
      if copied_info:
        run_process([iwilldolist.get_mergetool(),
                     get_item_path(item),
                     fixup_upstream_path(copied_info['copied_from_path'])],
                    iwilldolist.get_reporoot(),
                    dont_block=True)


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


class WillDoListItemShowPanelCommand(sublime_plugin.TextCommand):
  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self._command_names = []
    self._index_to_command = {}
    # Create an array of actions and a mapping from index to the action.
    index = 0
    for command_array in COMMANDS:
      self._command_names.append(
          '%s (%s)' % (command_array[1], command_array[0]))
      self._index_to_command[index] = command_array[2]
      index += 1

  def _on_done(self, index):
    if index in self._index_to_command:
      self.view.run_command(self._index_to_command[index])

  def run(self, edit):
    self.view.window().show_quick_panel(self._command_names, self._on_done)


class EventObserver(sublime_plugin.EventListener):
  def on_activated(self, view):
    # Reuse existing IWillDo view.
    if (view.settings().has('is_will_do_list_view')
            and iwilldolist.get_view() is None):
      view.run_command('will_do_list_start_update_interval')

  def on_pre_close(self, view):
    iwilldolist.on_view_closing(view)


class IWillDoList(object):
  """Global class that controls the plugin."""

  class NetworkWorkerThread(threading.Thread):
    def __init__(self, url, method, data, auth_token, callback, repeating):
      super(IWillDoList.NetworkWorkerThread, self).__init__()
      self._url = url
      self._method = method
      self._data = data
      self._callback = callback
      self._repeating = repeating
      self._headers = {
          'Content-Type': 'application/json',
          'Authorization': 'Token %s' % auth_token
      }
      self._stop_event = threading.Event()

    def _fetch_url(self, url):
      try:
        data = urllib.request.urlopen(url).read().decode('utf-8')
      except urllib.error.URLError as ex:
        data = '{"error": "Failed retrieving data. %s"}' % ex.reason
      return data

    def stop(self):
      self._stop_event.set()

    def run(self):
      while True:
        request = urllib.request.Request(self._url,
                                         data=self._data,
                                         headers=self._headers,
                                         method=self._method)
        data = json.loads(self._fetch_url(request))
        sublime.set_timeout(partial(self._callback, data))
        if self._repeating:
          sleep(CHECK_INTERVAL_SEC)
        if not self._repeating or self._stop_event.is_set():
          break

  class CopiedInfoFetcherFileIOThread(threading.Thread):
    def __init__(self, file_paths, reporoot, callback):
      super(IWillDoList.CopiedInfoFetcherFileIOThread, self).__init__()
      self._file_paths = file_paths
      self._reporoot = reporoot
      self._callback = callback

    def run(self):
      data = {}
      for file_path in self._file_paths:
        if os.path.exists(file_path):
          data[file_path] = CopiedFile.create(
              file_path, self._reporoot, allow_caching=False)
      sublime.set_timeout(partial(self._callback, data))

  def __init__(self):
    # The View that is currently showing the IWillDo list. Only one such view
    # can exist at a time.
    self._view = None
    # Mapping from the line number in generated IWillDo list to an item object.
    self._line_to_item_mapping = {}
    # A dictionary of path: CopiedInfo values.
    self._copied_info_data = {}
    self._username = ''
    self._auth_token = ''
    self._reporoot = ''
    self._upstream_sha = ''
    self._repeating_thread = None
    self._initialized = False

  def __del__(self):
    self._view = None
    self._stop_repeating_thread_if_started()

  def _stop_repeating_thread_if_started(self):
    if self._repeating_thread:
      self._repeating_thread.stop()
      self._repeating_thread = None

  def initialize(self, view):
    self._view = view
    self._view.settings().set('is_will_do_list_view', True)
    self._username = view.settings().get(PREF_NAME_USERNAME)
    self._reporoot = view.settings().get(PREF_NAME_REPOROOT)
    self._auth_token = view.settings().get(PREF_NAME_AUTHTOKEN)
    self._mergetool = view.settings().get(PREF_NAME_MERGETOOL, 'p4merge')
    if not self._initialized:
      # Import libintake package from the desktop tools dir. We have to do it
      # only once. We must also make the imported module be visible in the
      # global scope.
      sys.path.append(
          os.path.join(self._reporoot, 'desktop', 'tools', 'libintake'))
      global CopiedFile
      from copied_file import CopiedFile
    self._initialized = True

  def get_view(self):
    return self._view

  def get_username(self):
    return self._username

  def get_reporoot(self):
    return self._reporoot

  def get_mergetool(self):
    return self._mergetool

  def get_copied_info_for_item(self, item):
    absolute_path = get_item_path(item)
    if absolute_path in self._copied_info_data:
      return self._copied_info_data[absolute_path]
    return None

  def get_line_to_item_mapping(self):
    return self._line_to_item_mapping

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
      self._stop_repeating_thread_if_started()

  def make_request(self, URL, method, data, callback):
    IWillDoList.NetworkWorkerThread(
        URL, method, data, self._auth_token, callback, False).start()

  def trigger_update(self, repeating=False):
    """Triggers update of the list on the network thread."""

    # Can't 100% rely on on_pre_close event firing. It doesn't when closing
    # window for example. Not sure if checking buffer_id is the official way for
    # checking if the view was closed...
    if self._view and self._view.buffer_id() == 0:
      self.on_view_closing(self._view)

    if self._view:
      thread = IWillDoList.NetworkWorkerThread(
          API_LATEST_LIST_URL,
          'GET',
          None,
          self._auth_token,
          IWillDoList.on_data_fetched,
          repeating)
      thread.start()
      if repeating:
        self._stop_repeating_thread_if_started()
        self._repeating_thread = thread

  @staticmethod
  def on_data_fetched(data):
    """Global callback function instead of a IWillDoList class member to avoid
       locking the class instance when it needs to be garbage collected."""

    iwilldolist.update_view_with_data(data)
    iwilldolist.update_copied_info_data(data)

  def update_view_with_data(self, data):
    if self._view:
      self._view.run_command('will_do_list_update_with_data', {'data': data})

  def update_copied_info_data(self, data):
    paths = []
    for group in data['groups']:
      for item in group['items']:
        paths.append(get_item_path(item))
    IWillDoList.CopiedInfoFetcherFileIOThread(
        paths, self._reporoot, self._on_copied_info_updated).start()

  def _on_copied_info_updated(self, data):
    self._copied_info_data = data
    if self._view:
      self._view.run_command('will_do_list_update_gutter_marks')


iwilldolist = IWillDoList()
