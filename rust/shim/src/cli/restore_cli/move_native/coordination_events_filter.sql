WHERE (seq = 1 AND event = 'bank_identity' AND agent_id = ''
    AND (actor = 'agent' OR (actor = 'daemon' AND principal = 'daemon'))
    AND payload = ('{"bank_id":' || (SELECT value::text FROM public.meta
        WHERE key = 'coordination_bank_id' AND jsonb_typeof(value) = 'string') || '}')) IS NOT TRUE
