# source-proxy-lane Specification

## Purpose
How an upstream client reaches the network: every client the fetch pipeline
drives must egress through the proxy selected for the current attempt, including
clients whose transport the pipeline's own patches do not cover, and must fall
back to direct egress when no proxy is selected.

## Requirements

### Requirement: An upstream client SHALL use the proxy the fetch was given

Every upstream client the fetch pipeline drives SHALL egress through the proxy
selected for the current attempt, including clients whose HTTP transport is not
covered by the `requests`/`httpx` patches. When no proxy has been selected, the
client SHALL fall back to direct egress without raising.

#### Scenario: yfinance honours the selected proxy

- **WHEN** a yfinance fetch runs inside an attempt whose proxy is set and that proxy reaches the endpoint
- **THEN** the request egresses through that proxy and returns data

#### Scenario: The proxy does not leak into the next fetch

- **WHEN** an attempt with proxy A is followed by an attempt with proxy B or none
- **THEN** the second attempt uses only its own proxy (or direct egress)

#### Scenario: Direct egress still works when no proxy is selected

- **WHEN** no proxy is available and the client runs without an override
- **THEN** the fetch proceeds direct as before and a missing proxy never becomes an error
