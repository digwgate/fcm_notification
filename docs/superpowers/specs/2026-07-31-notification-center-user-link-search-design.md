# Notification Center User Link Search Design

## Context

The Notification Center's Individual Users `MultiSelectList` currently calls
`get_enabled_user_options`. That endpoint reads every enabled User Device,
collects every distinct device owner, reads all matching User records, sorts and
filters them in Python, and only then stops at 20 results. On sites with thousands
of users or devices, opening the selector can exceed the web request timeout.

Frappe Link fields avoid this pattern. They send the current search text to
`frappe.desk.search.search_link`, run a bounded database query, and return only a
small page of autosuggest rows.

## Goals

- Make Individual Users behave like a native Frappe Link search while retaining
  multi-selection.
- Show the first configured Link-field result page when the selector opens.
- Search by user ID, full name, or email as the operator types.
- Return only enabled users that have at least one enabled device matching the
  optional platform filter.
- Apply filtering, deduplication, ordering, and pagination in the database so
  the rows materialized in Python and transferred to the browser remain bounded.
- Preserve the existing recipient preview and notification sending behavior.

## Non-goals

- Changing role or User Group selectors.
- Paginating the recipient preview or changing notification dispatch.
- Adding new Notification Center settings or changing User Device data.
- Refactoring the bulk recipient collection used after targets are selected.

## Design

### Desk control

The Individual Users field remains a `MultiSelectList`. Its `get_data(txt)`
callback will call `frappe.desk.search.search_link` with:

- `doctype`: `User`
- `txt`: the current debounced input text
- `query`: a new Notification Center custom Link query
- `filters`: the currently selected platform, when present
- `page_length`: Frappe's configured Link-field results limit, falling back to 10

The native search endpoint already returns the `{value, label, description}`
shape consumed by `MultiSelectList`. Opening the dropdown with blank text returns
the first page; typing replaces it with a bounded server-side search page.
Existing selected values remain managed by `ControlMultiSelectList`.

### Custom Link query

A whitelisted, search-input-sanitized method will implement Frappe's custom Link
query signature:

`(doctype, txt, searchfield, start, page_len, filters, ...)`

The method will require the System Manager role, normalize the optional platform,
and query `User` joined to `User Device`. The query will:

- require `User.enabled = 1` and `User Device.enabled = 1`;
- require a non-empty device token;
- optionally require the normalized platform;
- match `txt` against User name, full name, or email;
- select distinct users to collapse multiple devices;
- order results deterministically by full name and user name;
- apply `OFFSET start` and `LIMIT page_len` before execution.

It will return Link-query tuples containing user name, full name, and email.
Frappe's `search_link` formatter will turn those tuples into autosuggest rows.
The old bulk option endpoint will no longer be used by the page.

### Data flow

1. The operator opens the Individual Users dropdown or types search text.
2. `ControlMultiSelectList` invokes its debounced `get_data` callback.
3. The callback calls Frappe `search_link` with the custom query and platform.
4. The custom query performs one bounded database join and returns one page.
5. Frappe formats the rows and the multi-select renders them.
6. Selecting a value continues to trigger the existing recipient preview call.

## Error and security behavior

- Only System Managers may execute the custom query.
- Frappe's search-input sanitizer protects the Link query parameters.
- Query Builder values remain parameterized; raw user text is not interpolated
  into SQL.
- An unsupported or empty platform is handled by the existing normalization
  behavior.
- No eligible match returns an empty option list without an error.

## Testing

The regression tests will cover:

- the custom query accepts Frappe Link parameters and returns the bounded rows;
- platform and search text are passed into database-side filtering;
- `start` and `page_len` control pagination;
- duplicate devices do not produce duplicate users;
- users without enabled matching devices are excluded;
- the option-search path never invokes the bulk `_get_enabled_device_rows` or
  `_get_user_details` helpers;
- the Desk field calls `frappe.desk.search.search_link` with `User`, the custom
  query path, platform filters, and the configured Link result limit.

Verification will include the focused pytest suite, the full app tests, Python
compilation, JavaScript syntax checking, and `git diff --check`.

## Compatibility

The submitted field value remains an array of User names, so recipient preview
and sending APIs require no payload changes. No DocType migration or data patch is
required.
