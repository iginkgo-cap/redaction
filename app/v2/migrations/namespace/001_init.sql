-- 每个 namespace 一个 database（ADR-007 §1）：entity 为实体与映射合一，counter 按 kind 分配编号
CREATE TABLE entity (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL,
    value_hmac  bytea NOT NULL,
    value_enc   bytea NOT NULL,
    placeholder text,
    seq         integer,
    source      text NOT NULL,
    suppressed  boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, value_hmac)
);

CREATE UNIQUE INDEX entity_placeholder_uq ON entity (placeholder) WHERE placeholder IS NOT NULL;

CREATE TABLE counter (
    kind text PRIMARY KEY,
    seq  integer NOT NULL
);
