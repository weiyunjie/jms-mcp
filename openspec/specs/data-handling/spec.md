# data-handling Specification

## Purpose

Ensure command output is delivered safely and usably by normalizing encodings to UTF-8, detecting binary streams, capping single-command output size, and delivering large batch results as downloadable compressed files.

## Requirements

### Requirement: Encoding normalization to UTF-8
The system SHALL NOT trust a declared output encoding and SHALL apply a detect-then-fallback-then-normalize pipeline that emits UTF-8.

#### Scenario: Detect and normalize non-UTF-8 text
- **WHEN** a host returns text output in an encoding such as GBK or Latin-1
- **THEN** system detects the encoding, decodes accordingly, and returns the output normalized to UTF-8

#### Scenario: Fallback when detection is uncertain
- **WHEN** encoding detection cannot confidently determine the source encoding
- **THEN** system applies a defined fallback decoding strategy and still emits valid UTF-8 rather than raising or returning corrupted bytes

#### Scenario: Already-UTF-8 output
- **WHEN** output is already valid UTF-8
- **THEN** system returns it unchanged

### Requirement: Binary stream detection
The system SHALL identify binary output and SHALL NOT treat it as a string.

#### Scenario: Recognize known binary-producing commands
- **WHEN** command output comes from tools that emit binary streams (tar, gzip/zip, openssl, compressed mysqldump)
- **THEN** system identifies the output as a binary stream and does not return it as plain text

#### Scenario: Recognize non-UTF-8 binary stdout
- **WHEN** stdout is not valid UTF-8 text after the encoding pipeline
- **THEN** system classifies it as a binary stream and handles it as binary (e.g. encoded or offered as a downloadable artifact) rather than as a string

### Requirement: Large single-command output cap
The system SHALL bound the size of a single command's returned output at a configurable maximum, defaulting to 100MB.

#### Scenario: Output exceeds the cap
- **WHEN** a command's output exceeds the configured maximum (default 100MB)
- **THEN** system returns the first 100MB of output and annotates both before and after the returned content that the output was truncated at the configured limit

#### Scenario: Output within the cap
- **WHEN** a command's output is at or below the configured maximum
- **THEN** system returns the full output without truncation annotations

#### Scenario: Cap is configurable
- **WHEN** the deployment configures a non-default maximum output size
- **THEN** system enforces that value instead of 100MB

### Requirement: Large batch result delivered as downloadable file
The system SHALL deliver large aggregated batch results as a compressed file with a download channel instead of returning them inline.

#### Scenario: Large aggregate result set
- **WHEN** the aggregate result set of a batch operation is large
- **THEN** system writes the results to a compressed file and returns a download reference rather than embedding the full results inline

#### Scenario: Small aggregate result set
- **WHEN** the aggregate result set is small enough to return inline
- **THEN** system returns the results directly without producing a download file
