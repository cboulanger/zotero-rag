# Public Web Interface

The backend can optionally expose a browser-accessible query UI at `/public/`
that lets anyone query a publicly readable Zotero library **without** the
Zotero plugin or an API key.

## 1. Make the Zotero library public

Set the library to *Public* in your Zotero.org account settings.

## 2. Create a config file

Use [`public-libraries.example.json`](../public-libraries.example.json) as a
template:

```json
{
  "users/1234567": {
    "title": "My Research Library",
    "description": "Papers on computational linguistics.",
    "placeholder": "e.g. What methods are used for cross-lingual transfer?"
  },
  "groups/9876543": {
    "title": "DH Working Group",
    "description": "Digital humanities reading list."
  }
}
```

Keys are Zotero.org library slugs (`users/{userId}` or `groups/{groupId}`).
The optional `placeholder` field customises the hint text in the question
input; `title` and `description` are shown on the query page.

## 3. Point the server at the file

Add this line to your `.env`:

```env
PUBLIC_LIBRARIES_CONFIG=/path/to/public-libraries.json
```

## 4. (Re)start the server

The UI is then available at:

| URL | Page |
| --- | --- |
| `/public/` | Index listing all configured libraries |
| `/public/users/{id}` | Query form + results for a user library |
| `/public/groups/{id}` | Query form + results for a group library |

Results include inline citations linked to the corresponding item on
`www.zotero.org`, with author/year labels fetched from the Zotero web API.
Libraries that are not listed in the config file return **403 Forbidden**.

> **Note:** The public UI only works for libraries that have already been
> indexed in **this running backend instance** via the Zotero plugin (or
> [automatic indexing](auto-indexing-setup.md)). It does not provide access
> to arbitrary public Zotero libraries — the library must first be indexed
> locally before queries can be answered.
