-- Phase 9b (D-A52): the engines whose results contained the source (a JSON list of engine keys: exa, perplexity, parallel, fetch), for the document's found_by line.
ALTER TABLE sources ADD COLUMN found_by_json TEXT;
