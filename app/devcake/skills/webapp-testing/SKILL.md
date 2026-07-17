---
name: webapp-testing
description: Toolkit for interacting with and testing local web applications using Playwright — verifying frontend functionality, debugging UI behavior, capturing browser screenshots, and viewing browser logs. Use when testing or debugging a local web app end-to-end in a real browser.
license: Apache-2.0
metadata:
  source: https://github.com/anthropics/skills
  author: Anthropic
---

# Web Application Testing

To test local web applications, write native Python Playwright scripts.

## Decision Tree: Choosing Your Approach

```
User task → Is it static HTML?
    ├─ Yes → Read HTML file directly to identify selectors
    │         ├─ Success → Write Playwright script using selectors
    │         └─ Fails/Incomplete → Treat as dynamic (below)
    │
    └─ No (dynamic webapp) → Is the server already running?
        ├─ No → Start the server yourself (see Server Lifecycle below),
        │        then write a simplified Playwright script
        │
        └─ Yes → Reconnaissance-then-action:
            1. Navigate and wait for networkidle
            2. Take screenshot or inspect DOM
            3. Identify selectors from rendered state
            4. Execute actions with discovered selectors
```

## Server Lifecycle

When the app server is not already running, manage it around your test script:

1. **Start the server in the background** and capture its logs:
   ```bash
   (cd frontend && npm run dev > /tmp/devserver.log 2>&1 &)
   ```
2. **Wait for the port to be ready** — poll instead of sleeping a fixed time:
   ```bash
   for i in $(seq 1 30); do curl -sf http://localhost:5173 >/dev/null && break; sleep 1; done
   ```
3. **Run your Playwright script** against the now-ready URL.
4. **Stop the server** when done (`kill` the background process) and check `/tmp/devserver.log` if anything failed.

For multi-server setups (e.g., backend + frontend), start each server the same way and wait on each port before running the automation.

The automation script itself should include only Playwright logic:

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True) # Always launch chromium in headless mode
    page = browser.new_page()
    page.goto('http://localhost:5173') # Server already running and ready
    page.wait_for_load_state('networkidle') # CRITICAL: Wait for JS to execute
    # ... your automation logic
    browser.close()
```

## Reconnaissance-Then-Action Pattern

1. **Inspect rendered DOM**:
   ```python
   page.screenshot(path='/tmp/inspect.png', full_page=True)
   content = page.content()
   page.locator('button').all()
   ```

2. **Identify selectors** from inspection results

3. **Execute actions** using discovered selectors

## Common Pitfall

- **Don't** inspect the DOM before waiting for `networkidle` on dynamic apps
- **Do** wait for `page.wait_for_load_state('networkidle')` before inspection

## Best Practices

- Use `sync_playwright()` for synchronous scripts
- Always close the browser when done
- Use descriptive selectors: `text=`, `role=`, CSS selectors, or IDs
- Add appropriate waits: `page.wait_for_selector()` or `page.wait_for_timeout()`

## Useful Patterns

- **Element discovery** — enumerate interactive elements when you don't know the page:
  ```python
  for el in page.locator('button, a, input, select').all():
      print(el.evaluate('e => e.outerHTML.slice(0, 200)'))
  ```
- **Static HTML via file:// URLs** — for local HTML files with no server, `page.goto(f'file://{os.path.abspath("index.html")}')`.
- **Console log capture** — attach the listener before navigating:
  ```python
  page.on('console', lambda msg: print(f'[{msg.type}] {msg.text}'))
  page.on('pageerror', lambda err: print(f'[pageerror] {err}'))
  ```

---
*Vendored from [anthropics/skills](https://github.com/anthropics/skills) (Apache-2.0). Modifications: bundled `scripts/with_server.py` helper and `examples/` files replaced with self-contained server-lifecycle instructions and inline pattern snippets; frontmatter adjusted for the devcake skill store.*
