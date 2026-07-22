-- MAS needs its own database, separate from Synapse's. Created here on first
-- cluster init (harmless if MAS is never enabled). Matches the cluster's UTF8/C.
-- Owned by the synapse role, which MAS reuses to connect.
CREATE DATABASE mas ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0 OWNER synapse;
