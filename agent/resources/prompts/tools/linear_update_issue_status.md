Move a Linear issue to another workflow status, such as "In Progress" or "In Review".

Pass the issue identifier (for example ENG-123) or id, and the status name, type, or id. Use `linear_list_issue_statuses` with the issue's team to see the valid statuses. This changes only the status; it cannot edit the title, description, assignee, or anything else.

Don't move an issue to a completed or canceled status unless the person asks: Linear's GitHub integration completes the issue when its pull request merges.
