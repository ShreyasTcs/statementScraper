CREATE TABLE documents (
	id VARCHAR(36) NOT NULL, 
	original_filename VARCHAR(255) NOT NULL, 
	stored_filename VARCHAR(255) NOT NULL, 
	file_type VARCHAR(16) NOT NULL, 
	mime_type VARCHAR(64) NOT NULL, 
	file_size_bytes INTEGER NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	page_count INTEGER NOT NULL, 
	has_text_layer BOOLEAN NOT NULL, 
	requires_ocr BOOLEAN NOT NULL, 
	preview_path VARCHAR(512) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE extraction_fields (
	id VARCHAR(36) NOT NULL, 
	extraction_id VARCHAR(36) NOT NULL, 
	field_name VARCHAR(64) NOT NULL, 
	display_order INTEGER NOT NULL, 
	value TEXT, 
	normalized_value TEXT, 
	value_type VARCHAR(24) NOT NULL, 
	confidence FLOAT, 
	confidence_status VARCHAR(16) NOT NULL, 
	validation_status VARCHAR(16) NOT NULL, 
	validation_message TEXT, 
	source_page INTEGER, 
	source_bbox JSON, 
	anchor_text TEXT, 
	candidates JSON, 
	reasons JSON, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(extraction_id) REFERENCES statement_extractions (id)
);

CREATE TABLE processing_runs (
	id VARCHAR(36) NOT NULL, 
	document_id VARCHAR(36) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	text_source VARCHAR(16), 
	ocr_used BOOLEAN NOT NULL, 
	structure_model VARCHAR(64), 
	started_at DATETIME, 
	completed_at DATETIME, 
	error_code VARCHAR(64), 
	error_message TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id)
);

CREATE TABLE statement_extractions (
	id VARCHAR(36) NOT NULL, 
	document_id VARCHAR(36) NOT NULL, 
	processing_run_id VARCHAR(36) NOT NULL, 
	overall_confidence FLOAT, 
	summary JSON, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id), 
	FOREIGN KEY(processing_run_id) REFERENCES processing_runs (id)
);

CREATE TABLE transactions (
	id VARCHAR(36) NOT NULL, 
	extraction_id VARCHAR(36) NOT NULL, 
	row_index INTEGER NOT NULL, 
	page INTEGER NOT NULL, 
	bbox JSON, 
	txn_date VARCHAR(10), 
	value_date VARCHAR(10), 
	date_inherited BOOLEAN NOT NULL, 
	description TEXT, 
	reference VARCHAR(128), 
	debit VARCHAR(32), 
	credit VARCHAR(32), 
	balance VARCHAR(32), 
	direction_inferred BOOLEAN NOT NULL, 
	balance_check VARCHAR(16) NOT NULL, 
	confidence FLOAT, 
	status VARCHAR(16) NOT NULL, 
	issues JSON, 
	reasons JSON, 
	signals JSON, 
	raw JSON, 
	PRIMARY KEY (id), 
	FOREIGN KEY(extraction_id) REFERENCES statement_extractions (id)
);
