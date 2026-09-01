# Reusable notes

- In SDK streaming classifiers, keep consumer-visible evidence scoped to one
  `ResultMessage`; exclude `parent_tool_use_id` subagent narration and reset at
  each result before evaluating duplicated terminal text.
