# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.2.1] - 2026-04-26

### Fixed
- Incorrect project URLs in package metadata

## [1.2] - 2026-04-14

### Fixed
- Infinite loop when response body is buffered by Python's `BufferedReader`
  - Replaced `select()`-based polling with socket timeout on `read1()`

## [1.1] - 2026-04-11

### Added
- `max_response_size` parameter to `Request` (default 5 MB) to limit response body size
- `ResponseTooLargeError` exception for oversized responses
- `select`-based body reading loop for responsive `close()` during transfer
- `IncompleteRead` detection when `Content-Length` does not match received bytes

### Changed
- `Response.body` is now `bytearray` instead of `bytes` to reduce peak memory usage
- Response body is read incrementally via `read1()` instead of a single `read()` call

## [1.0] - 2026-04-10

- Initial release
