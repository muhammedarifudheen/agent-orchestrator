# Bookshelf lookup service (imaginary)

A small read-only catalogue. No sign-in is needed. Base address:

    https://api.bookshelf.example/v1

## Fetch one book

GET `/books/{isbn}` returns the catalogue entry for the book with that ISBN. The `isbn` value is required
and goes in the path. Sample reply:

    {"isbn": "9780140449136", "title": "The Odyssey", "year": 1996}

## Find authors

GET `/authors` looks up authors. The optional query parameter `name` filters by part of the author's name.
The reply format is described only loosely: it is a list of author records.
