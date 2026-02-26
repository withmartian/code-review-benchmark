mod compute;
mod db;
mod handlers;
mod model;
#[cfg(test)]
mod tests;

use std::sync::Arc;

use axum::Router;
use axum::routing::get;
use tower_http::cors::CorsLayer;
use tracing::info;
use tracing_subscriber::prelude::*;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let filter = EnvFilter::from_default_env().add_directive("info".parse()?);

    // Cloud Run sets K_SERVICE — use structured JSON there, human-readable locally
    if std::env::var("K_SERVICE").is_ok() {
        tracing_subscriber::registry()
            .with(filter)
            .with(tracing_stackdriver::layer())
            .init();
    } else {
        tracing_subscriber::fmt()
            .with_env_filter(filter)
            .init();
    }

    let database_url =
        std::env::var("DATABASE_URL").expect("DATABASE_URL environment variable required");
    let bind_addr = std::env::var("BIND_ADDR").unwrap_or_else(|_| "0.0.0.0:3000".to_string());

    info!("Loading data from Postgres...");
    let snapshot = db::load_from_postgres(&database_url).await?;
    let total_records: usize = snapshot.by_date.values().map(|v| v.len()).sum::<usize>()
        + snapshot.no_date.len();
    info!("Loaded {total_records} records into memory");

    let state = Arc::new(snapshot);

    let app = Router::new()
        .route("/", get(handlers::dashboard))
        .route("/api/options", get(handlers::options_handler))
        .route("/api/daily-metrics", get(handlers::daily_metrics))
        .route("/api/leaderboard", get(handlers::leaderboard_handler))
        .route("/api/volumes", get(handlers::volumes_handler))
        .route("/up", get(|| async { "ok" }))
        .layer(CorsLayer::permissive())
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(&bind_addr).await?;
    info!("Listening on {bind_addr}");
    axum::serve(listener, app).await?;

    Ok(())
}
