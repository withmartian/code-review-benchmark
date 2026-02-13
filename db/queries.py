"""All SQL queries as named constants."""

# -- Chatbots ------------------------------------------------------------------

UPSERT_CHATBOT = """
    INSERT INTO chatbots (github_username, display_name)
    VALUES ($1, $2)
    ON CONFLICT (github_username) DO UPDATE SET display_name = $2
"""

GET_CHATBOT_BY_USERNAME = """
    SELECT id, github_username, display_name, created_at
    FROM chatbots WHERE github_username = $1
"""

GET_ALL_CHATBOTS = """
    SELECT id, github_username, display_name, created_at FROM chatbots
"""

# -- PRs -----------------------------------------------------------------------

INSERT_PR = """
    INSERT INTO prs (chatbot_id, repo_name, pr_number, pr_url, pr_title, pr_author,
                     pr_created_at, pr_merged, status, bq_events, bot_reviewed_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    ON CONFLICT (chatbot_id, repo_name, pr_number) DO NOTHING
"""

GET_PR = """
    SELECT id, chatbot_id, repo_name, pr_number, status, enrichment_step
    FROM prs WHERE chatbot_id = $1 AND repo_name = $2 AND pr_number = $3
"""

GET_PR_BY_ID = """
    SELECT * FROM prs WHERE id = $1
"""

GET_PENDING_PRS = """
    SELECT * FROM prs
    WHERE chatbot_id = $1
      AND status IN ('pending', 'enriching')
      AND enrichment_step IS DISTINCT FROM 'done'
    ORDER BY discovered_at ASC
    LIMIT $2
"""

# SQLite version (no IS DISTINCT FROM)
GET_PENDING_PRS_SQLITE = """
    SELECT * FROM prs
    WHERE chatbot_id = $1
      AND status IN ('pending', 'enriching')
      AND (enrichment_step IS NULL OR enrichment_step != 'done')
    ORDER BY discovered_at ASC
    LIMIT $2
"""

GET_ASSEMBLED_PRS_NOT_ANALYZED = """
    SELECT p.* FROM prs p
    LEFT JOIN llm_analyses la ON la.pr_id = p.id AND la.chatbot_id = p.chatbot_id
    WHERE p.chatbot_id = $1
      AND p.status = 'assembled'
      AND la.id IS NULL
    ORDER BY p.bot_reviewed_at DESC NULLS LAST
    LIMIT $2
"""

GET_ALL_ASSEMBLED_NOT_ANALYZED = """
    SELECT p.* FROM prs p
    LEFT JOIN llm_analyses la ON la.pr_id = p.id AND la.chatbot_id = p.chatbot_id
    WHERE p.status = 'assembled'
      AND la.id IS NULL
    ORDER BY p.bot_reviewed_at DESC NULLS LAST
    LIMIT $1
"""

GET_ASSEMBLED_PRS_NOT_ANALYZED_SINCE = """
    SELECT p.* FROM prs p
    LEFT JOIN llm_analyses la ON la.pr_id = p.id AND la.chatbot_id = p.chatbot_id
    WHERE p.chatbot_id = $1
      AND p.status = 'assembled'
      AND la.id IS NULL
      AND p.bot_reviewed_at >= $2
    ORDER BY p.bot_reviewed_at DESC NULLS LAST
    LIMIT $3
"""

GET_ALL_ASSEMBLED_NOT_ANALYZED_SINCE = """
    SELECT p.* FROM prs p
    LEFT JOIN llm_analyses la ON la.pr_id = p.id AND la.chatbot_id = p.chatbot_id
    WHERE p.status = 'assembled'
      AND la.id IS NULL
      AND p.bot_reviewed_at >= $1
    ORDER BY p.bot_reviewed_at DESC NULLS LAST
    LIMIT $2
"""

# -- PR locking ----------------------------------------------------------------

LOCK_PR = """
    UPDATE prs SET locked_by = $1, locked_at = $2, status = 'enriching'
    WHERE id = $3 AND (locked_by IS NULL OR locked_at < $4)
"""

UNLOCK_PR = """
    UPDATE prs SET locked_by = NULL, locked_at = NULL WHERE id = $1
"""

# -- PR enrichment updates -----------------------------------------------------

UPDATE_PR_BQ_EVENTS = """
    UPDATE prs SET bq_events = $1, enrichment_step = 'bq_events' WHERE id = $2
"""

UPDATE_PR_COMMITS = """
    UPDATE prs SET commits = $1, enrichment_step = 'commits' WHERE id = $2
"""

UPDATE_PR_REVIEWS = """
    UPDATE prs SET reviews = $1, enrichment_step = 'reviews' WHERE id = $2
"""

UPDATE_PR_THREADS = """
    UPDATE prs SET review_threads = $1, enrichment_step = 'threads' WHERE id = $2
"""

UPDATE_PR_COMMIT_DETAILS = """
    UPDATE prs SET commit_details = $1, diff_lines = $2, enrichment_step = 'details' WHERE id = $3
"""

UPDATE_PR_DIFF_LINES = """
    UPDATE prs SET diff_lines = $1 WHERE id = $2
"""

GET_PRS_MISSING_DIFF_LINES = """
    SELECT id, commit_details FROM prs
    WHERE commit_details IS NOT NULL AND diff_lines IS NULL
    LIMIT $1
"""

COUNT_PRS_MISSING_DIFF_LINES = """
    SELECT COUNT(*) as count FROM prs
    WHERE commit_details IS NOT NULL AND diff_lines IS NULL
"""

UPDATE_PR_ENRICHMENT_DONE = """
    UPDATE prs SET enrichment_step = 'done', status = 'enriched',
                   enriched_at = $1, locked_by = NULL, locked_at = NULL
    WHERE id = $2
"""

UPDATE_PR_ASSEMBLED = """
    UPDATE prs SET assembled = $1, status = 'assembled', assembled_at = $2
    WHERE id = $3
"""

UPDATE_PR_ERROR = """
    UPDATE prs SET status = 'error', error_message = $1,
                   locked_by = NULL, locked_at = NULL
    WHERE id = $2
"""

MARK_PR_SKIPPED = """
    UPDATE prs SET status = 'skipped', error_message = $1,
                   locked_by = NULL, locked_at = NULL
    WHERE id = $2
"""

UPDATE_PR_METADATA = """
    UPDATE prs SET pr_title = $1, pr_author = $2, pr_created_at = $3, pr_merged = $4
    WHERE id = $5
"""

# -- LLM analyses --------------------------------------------------------------

INSERT_LLM_ANALYSIS = """
    INSERT INTO llm_analyses (pr_id, chatbot_id, bot_suggestions, human_actions,
                              matching_results, total_bot_comments, matched_bot_comments,
                              precision, recall, f_beta, model_name)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    ON CONFLICT (pr_id, chatbot_id) DO UPDATE SET
        bot_suggestions = $3, human_actions = $4, matching_results = $5,
        total_bot_comments = $6, matched_bot_comments = $7,
        precision = $8, recall = $9, f_beta = $10, model_name = $11,
        analyzed_at = CURRENT_TIMESTAMP
"""

UPDATE_PR_ANALYZED = """
    UPDATE prs SET status = 'analyzed', analyzed_at = $1 WHERE id = $2
"""

# -- PR labels -----------------------------------------------------------------

INSERT_PR_LABELS = """
    INSERT INTO pr_labels (pr_id, chatbot_id, labels, model_name)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (pr_id, chatbot_id) DO UPDATE SET
        labels = $3, model_name = $4, labeled_at = CURRENT_TIMESTAMP
"""

GET_ANALYZED_NOT_LABELED = """
    SELECT p.*, la.bot_suggestions, la.matching_results
    FROM prs p
    JOIN llm_analyses la ON la.pr_id = p.id AND la.chatbot_id = p.chatbot_id
    LEFT JOIN pr_labels pl ON pl.pr_id = p.id AND pl.chatbot_id = p.chatbot_id
    WHERE p.chatbot_id = $1
      AND p.status = 'analyzed'
      AND pl.id IS NULL
    ORDER BY p.bot_reviewed_at DESC NULLS LAST
    LIMIT $2
"""

GET_ALL_ANALYZED_NOT_LABELED = """
    SELECT p.*, la.bot_suggestions, la.matching_results
    FROM prs p
    JOIN llm_analyses la ON la.pr_id = p.id AND la.chatbot_id = p.chatbot_id
    LEFT JOIN pr_labels pl ON pl.pr_id = p.id AND pl.chatbot_id = p.chatbot_id
    WHERE p.status = 'analyzed'
      AND pl.id IS NULL
    ORDER BY p.bot_reviewed_at DESC NULLS LAST
    LIMIT $1
"""

GET_ANALYZED_NOT_LABELED_SINCE = """
    SELECT p.*, la.bot_suggestions, la.matching_results
    FROM prs p
    JOIN llm_analyses la ON la.pr_id = p.id AND la.chatbot_id = p.chatbot_id
    LEFT JOIN pr_labels pl ON pl.pr_id = p.id AND pl.chatbot_id = p.chatbot_id
    WHERE p.chatbot_id = $1
      AND p.status = 'analyzed'
      AND pl.id IS NULL
      AND p.bot_reviewed_at >= $2
    ORDER BY p.bot_reviewed_at DESC NULLS LAST
    LIMIT $3
"""

GET_ALL_ANALYZED_NOT_LABELED_SINCE = """
    SELECT p.*, la.bot_suggestions, la.matching_results
    FROM prs p
    JOIN llm_analyses la ON la.pr_id = p.id AND la.chatbot_id = p.chatbot_id
    LEFT JOIN pr_labels pl ON pl.pr_id = p.id AND pl.chatbot_id = p.chatbot_id
    WHERE p.status = 'analyzed'
      AND pl.id IS NULL
      AND p.bot_reviewed_at >= $1
    ORDER BY p.bot_reviewed_at DESC NULLS LAST
    LIMIT $2
"""

# -- Dashboard queries ---------------------------------------------------------

GET_ANALYSES_BY_CHATBOT = """
    SELECT la.*, p.repo_name, p.pr_number, p.pr_url, p.pr_created_at,
           c.github_username, c.display_name
    FROM llm_analyses la
    JOIN prs p ON la.pr_id = p.id
    JOIN chatbots c ON la.chatbot_id = c.id
    WHERE la.chatbot_id = $1
    ORDER BY la.analyzed_at DESC
"""

GET_ALL_ANALYSES = """
    SELECT la.*, p.repo_name, p.pr_number, p.pr_url, p.pr_created_at,
           c.github_username, c.display_name
    FROM llm_analyses la
    JOIN prs p ON la.pr_id = p.id
    JOIN chatbots c ON la.chatbot_id = c.id
    ORDER BY la.analyzed_at DESC
"""

GET_PR_STATUS_COUNTS = """
    SELECT status, COUNT(*) as count FROM prs
    WHERE chatbot_id = $1
    GROUP BY status
"""

GET_ALL_PR_STATUS_COUNTS = """
    SELECT c.github_username, p.status, COUNT(*) as count
    FROM prs p
    JOIN chatbots c ON p.chatbot_id = c.id
    GROUP BY c.github_username, p.status
"""
