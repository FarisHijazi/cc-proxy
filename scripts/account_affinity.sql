-- Run explicitly in a dedicated application database before opting in.
CREATE TABLE IF NOT EXISTS ccp_affinity (
    namespace text PRIMARY KEY,
    state jsonb NOT NULL DEFAULT '{"sessions": {}, "turn": -1}'::jsonb
);
