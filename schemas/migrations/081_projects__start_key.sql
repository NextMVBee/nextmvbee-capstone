-- migration 081 (Phase 19 package 19.4, D-A451; M5 of the founder actions sweep): projects gains start_key, the key of the Start form that
-- created the project. A repeated submission of the same form (a double press, the back button and a re-post, a copied form) returns the
-- project and run the first submission created and starts nothing new; the key is stored with the project so that the answer holds across a
-- restart. Every project created before it carries NULL and matches no form. Forward only. Applied by migrate() when projects lacks
-- start_key; the unique index (one project per form of an account) is created by schemas/schema.sql, which migrate() applies after the columns.
ALTER TABLE projects ADD COLUMN start_key TEXT;
