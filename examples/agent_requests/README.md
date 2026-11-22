# Example requests

Plain-text requests for the planning agent: demos of what a person can ask, and test cases. Edit them or add
your own; any `NN_name.txt` in this folder is picked up. The agent's code is in `src/agent/`.

```
# Notes for people: what the example shows and what a good answer looks like. Not sent to Claude.
# expect: plan          (or: no plan) whether Claude should hand in a spec
The request itself, as a person would type it. It can span several lines.
```

```
python -m agent --list                      # the examples and what each expects
python -m agent --example 03 --offline      # run one (by number or name); the API call is real
```

`--offline` plans against the demo deck: a full rack in bay 3, an empty one in bay 8, nothing else. Without it
the deck comes from the running controller, so an example can expect something the live deck does not have.
