## ADDED Requirements

### Requirement: Streamable HTTP transport endpoint
The system SHALL expose the MCP server over the MCP Streamable HTTP transport at a single configured mount path, accepting the transport's `GET`, `POST`, and `DELETE` methods on that path.

#### Scenario: Client opens a stream
- **WHEN** a client sends `GET` to the mount path with a valid Bearer token
- **THEN** the system establishes a Streamable HTTP stream and returns the negotiated session identifier in the `Mcp-Session-Id` response header

#### Scenario: Client posts a JSON-RPC message
- **WHEN** a client sends `POST` to the mount path with a JSON-RPC request body and a valid session
- **THEN** the system dispatches the message to the MCP server and returns the JSON-RPC response

#### Scenario: Client terminates a session
- **WHEN** a client sends `DELETE` to the mount path with an established `Mcp-Session-Id`
- **THEN** the system releases the session's resources and stops accepting messages for that session

#### Scenario: Method not supported on mount path
- **WHEN** a client sends an HTTP method other than `GET`, `POST`, or `DELETE` to the mount path
- **THEN** the system rejects the request without dispatching it to the MCP server

### Requirement: Bearer authentication on the transport
The system SHALL require a valid `Authorization: Bearer <api_key>` header on transport requests when an API key is configured, and SHALL reject requests that do not present it.

#### Scenario: Missing or malformed credentials
- **WHEN** an API key is configured and a transport request arrives without a Bearer token, or with a token that does not match the configured key
- **THEN** the system responds with HTTP 401 and does not establish a session or dispatch any message

#### Scenario: Valid credentials
- **WHEN** an API key is configured and a transport request presents the matching Bearer token
- **THEN** the system authorizes the request and proceeds with the transport exchange

#### Scenario: Session continuation after authentication
- **WHEN** a client has authenticated and established a session, and sends a follow-up request carrying its `Mcp-Session-Id`
- **THEN** the system authorizes the follow-up request using the same Bearer credential rather than a transport-specific session query parameter

### Requirement: Stateful session management over HTTP
The system SHALL maintain per-client session state across Streamable HTTP requests, keyed by the transport session identifier, so multi-message exchanges from the same client are routed to the same server session.

#### Scenario: Multiple messages in one session
- **WHEN** a client sends several `POST` messages carrying the same `Mcp-Session-Id`
- **THEN** the system routes them to the same MCP server session in order

#### Scenario: Unknown session identifier
- **WHEN** a `POST` or `DELETE` carries an `Mcp-Session-Id` that is not an active session
- **THEN** the system rejects the request rather than creating an implicit session

## REMOVED Requirements

### Requirement: Legacy SSE transport endpoint
**Reason**: Replaced by the MCP Streamable HTTP transport, which the MCP specification now positions as the standard. Maintaining a second transport doubles the auth and session-handling surface for no operator benefit in this deployment.
**Migration**: Clients previously connecting via `GET /sse` (SSE handshake) plus `POST /sse/messages/?session_id=...` MUST reconnect using the Streamable HTTP transport at the configured mount path (`GET`/`POST`/`DELETE`), supplying the same `Authorization: Bearer <api_key>` header. The `?session_id=` query parameter is no longer used; the transport session is carried by the `Mcp-Session-Id` header.
