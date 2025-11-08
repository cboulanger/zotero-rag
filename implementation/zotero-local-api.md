# Zotero Local API

Zotero provides a reasonably complete local implementation of the [Zotero API (api.zotero.org)](https://www.zotero.org/support/dev/web_api/v3/start).

Endpoints are accessible on the local server (localhost:23119 by default) under /api/.

Limitations compared to api.zotero.org:

- Only API version 3 (https://www.zotero.org/support/dev/web_api/v3/basics) is supported, and only
  one API version will ever be supported at a time. If a new API version is released and your
  client needs to maintain support for older versions, first query /api/ and read the
  Zotero-API-Version response header, then make requests conditionally.
- Write access is not yet supported.
- No authentication.
- No access to user data for users other than the local logged-in user. Use user ID 0 or the user's
  actual API user ID (https://www.zotero.org/settings/keys).
- Minimal access to metadata about groups.
- Atom is not supported.
- Item type/field endpoints (https://www.zotero.org/support/dev/web_api/v3/types_and_fields) will
  return localized names in the user's locale. The locale query parameter is not supported. The
  single exception is /api/creatorFields, which follows the web API's behavior in always returning
  results in English, *not* the user's locale.
- If your code relies on any undefined behavior or especially unusual corner cases in the web API,
  it'll probably work differently when using the local API. This implementation is primarily
  concerned with matching the web API's spec and secondarily with matching its observed behavior,
  but it does not make any attempt to replicate implementation details that your code might rely on.
  Sort orders might differ, quicksearch results will probably differ, and JSON you get from the
  local API is never going to be exactly identical to what you would get from the web API.

That said, there are benefits:

- Pagination is often unnecessary because the API doesn't mind sending you many megabytes of data
  at a time - nothing ever touches the network. For that reason, returned results are not limited
  by default (unlike in the web API, which has a default limit of 25 and will not return more than
  100 results at a time).
- For the same reason, no rate limits, and it's really fast.
- <userOrGroupPrefix>/searches/:searchKey/items returns the set of items matching a saved search
  (unlike in the web API, which doesn't support actually executing searches).