# Dashboard

Open Carrie On from Applications or visit `http://127.0.0.1:8766`. The dashboard uses the same coral C-arrow icon as the app. Search and select local chats, optionally set a continuation instruction, and queue the selection. Inspect reset deadlines and waiting/attention messages, remove enrollments, pause or resume the queue, and review recent history. Status refreshes every ten seconds; Refresh also updates usage and chat search. Closing the browser does not stop the worker.

Install using `python3 tokenmaxxer_ui.py install`; see the [usage guide](../README.md#use-the-interface) for repair and removal. The UI service is separate from the dispatch worker. It uses the existing locked queue through CLI command functions and never dispatches a turn itself.

`python3 -m unittest -v` includes real HTTP tests against an isolated queue and synthetic Codex responses. These cover enrollment, duplicate rejection, prompt preservation, pause/resume, removal/history, malformed requests, cross-origin rejection, and static-file boundaries. Browser checks should use synthetic chat data; preserve the production queue.
