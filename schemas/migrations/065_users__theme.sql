-- UI-3 (the Daylight Hive): the account's theme choice: light, dark, or NULL to follow the device. Applied by migrate() when users lacks theme.
ALTER TABLE users ADD COLUMN theme TEXT CHECK (theme IN ('light', 'dark'));
