# General Coding Guidelines

## Python Environment

- **Python Version**: 3.12 (downgraded from 3.13 due to PyTorch compatibility issues on Windows)
- **Package Manager**: Always use `uv` for all Python operations
- **Virtual Environment**: All Python commands must be executed within the uv-managed virtual environment
  - Use `uv run <command>` for one-off commands
  - Use `uv pip install <package>` for installing dependencies
  - Never use global Python or pip directly

## Testing

### Python Tests

- Use Python's built-in `unittest` framework
- Test files should be named `test_*.py` and placed in a `tests/` directory
- Run tests with: `uv run pytest` (after installing pytest) or `uv run python -m unittest discover`
- Aim for comprehensive coverage of all library methods and services
- Write tests before or alongside implementation (TDD encouraged)

### Node.js Tests

- Use Node.js built-in test runner (available in Node 23)
- Test files should be named `*.test.js` or placed in a `test/` directory
- Run tests with: `node --test`
- Test all plugin UI interactions and API communication logic

## Code Organization

- **Modularity**: Organize code into reusable modules to keep business logic clean and lightweight
- **Separation of Concerns**: Separate API routes, business logic, data access, and utilities into distinct modules
- **Single Responsibility**: Each module/class should have a single, well-defined purpose
- **DRY Principle**: Avoid code duplication by extracting common functionality into shared utilities

## Project Structure

### Backend (Python/FastAPI)

```
backend/
├── api/              # FastAPI routes and endpoint handlers
├── services/         # Core business logic (embeddings, LLM, RAG)
├── models/           # Pydantic models and data schemas
├── db/               # Database interfaces and repositories
├── utils/            # Shared utilities and helpers
├── tests/            # Unit and integration tests
└── pyproject.toml    # UV project configuration
```

**Note:** Environment configuration template is at project root: `.env.dist`

### Plugin (Node.js/JavaScript)

```
plugin/
├── src/
│   ├── bootstrap.js      # Plugin lifecycle
│   ├── ui/               # Dialog and UI components
│   ├── api/              # Backend communication
│   ├── zotero/           # Zotero API interactions
│   └── utils/            # Shared utilities
├── locale/               # Localization files
├── test/                 # Plugin tests
├── manifest.json         # Plugin manifest
└── package.json          # Node.js dependencies
```

## Zotero Plugin Development

### UI Development - Pragmatic Approach

**XUL (XML User Interface Language) is deprecated in Firefox but still functional in Zotero 7/8.**

**Recommended Approach:**
- Use **HTML elements with `html:` namespace** when possible for future-proofing
- **XUL is acceptable** for dialogs, preferences, and UI components if it simplifies development
- Prefer HTML for new code, but don't block on XUL if it works
- Focus on functionality over ideological purity

**Common Patterns:**
- XUL `<dialog>` with HTML children using `html:` namespace works well
- Mix XUL layout (`<vbox>`, `<hbox>`) with HTML form elements
- Use `createXULElement()` for menu items and structural elements
- Use `html:` prefix for form inputs, labels, buttons when feasible

### Dialog and Window Creation

For creating dialog windows in Zotero plugins:
1. Use XUL `<dialog>` or `<window>` as root element for `window.openDialog()`
2. Mix HTML elements (with `html:` namespace) for form controls
3. Apply styles using standard CSS files
4. Reference working plugin examples in `zotero-addons/` directory

## Code Quality

- **Type Hints**: Use Python type hints for all function signatures and class attributes
- **Docstrings**: Document all public functions, classes, and modules using clear docstrings
- **Error Handling**: Implement proper error handling with specific exception types
- **Logging**: Use appropriate logging levels (DEBUG, INFO, WARNING, ERROR) for operational visibility
- **Code Style**: Follow PEP 8 for Python, Standard JavaScript style for Node.js
- **Console Output**: Avoid Unicode emoji characters (✅ ❌ ➜ etc.) in print statements as they cause UnicodeEncodeError on Windows. Use ASCII alternatives like `[PASS]`, `[FAIL]`, `->` instead

## Version Control

- Write clear, descriptive commit messages
- Make atomic commits that represent single logical changes
- Reference issues/tasks in commit messages when applicable

## Debugging

- if you insert code that is only for debugging, mark it as such so that it can be easily idenitified and removed after the code has been fixed (e.g., by a `# DEBUG` trailing comment or `# BEGIN DEBUG`/`# END DEBUG` header and footer for longer code fragments).

## Documentation

- Maintain up-to-date README.md files in each major directory
- Document API endpoints with request/response examples
- Include setup and installation instructions
- Document configuration options and environment variables
- Keep inline comments focused on "why" rather than "what"
- In Javascript files, use TypeScript-compatible JSDOC annotations throughout for typing variables and documenting function parameters. Use the full power or typescript embedded in JSDoc, don't use generic types. Remember this is plain javascript, don't use Typescript directly. 

## Implementation progress documentation

- When implementing the master implementation plan,  create an document for each phase where you document what has been implemented and, after a phase is complete,  add short summary at the end of `master.md` and link to this document.
- If a step in a phase is complex, document that step separately. The master and the implementation documents should allow you to resume work in separate sessions any time.

## Security

- Never commit secrets, API keys, or credentials to version control
- Use environment variables for all sensitive configuration
- Validate and sanitize all user inputs
- Use parameterized queries to prevent injection attacks
- Keep dependencies updated to patch security vulnerabilities

## Performance

- Implement batch processing for large datasets
- Use async/await for I/O-bound operations
- Consider caching for frequently accessed data
- Profile code to identify bottlenecks before optimizing
- Document performance considerations for resource-intensive operations

## Calling command line utilities

- Remember or check what platform you are running on to generate the right CLI commands (e.g. Windows PowerShell vs. Mac ZSH or Linux bash)
- When using python on the command line, always use `uv run python`
- For more complex tasks, create a python script in the `scripts` dir and run it. 