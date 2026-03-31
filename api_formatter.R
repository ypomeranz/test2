#!/usr/bin/env Rscript

# API Endpoint Formatter
# Usage: Rscript api_formatter.R <url>
#        or run interactively and enter URL when prompted

# --- Package setup ---
required_packages <- c("httr", "jsonlite")
for (pkg in required_packages) {
  if (!requireNamespace(pkg, quietly = TRUE)) {
    message(sprintf("Installing missing package: %s", pkg))
    install.packages(pkg, repos = "https://cloud.r-project.org", quiet = TRUE)
  }
}
suppressPackageStartupMessages({
  library(httr)
  library(jsonlite)
})

# --- Helpers ---

# Pretty-print a nested list/vector with indentation
pretty_print <- function(x, indent = 0) {
  pad <- strrep("  ", indent)

  if (is.list(x)) {
    nms <- names(x)
    for (i in seq_along(x)) {
      key <- if (!is.null(nms) && nms[i] != "") nms[i] else paste0("[", i, "]")
      val <- x[[i]]
      if (is.list(val) && length(val) > 0) {
        cat(sprintf("%s\033[1;36m%s\033[0m:\n", pad, key))
        pretty_print(val, indent + 1)
      } else if (is.null(val)) {
        cat(sprintf("%s\033[1;36m%s\033[0m: \033[90mnull\033[0m\n", pad, key))
      } else if (is.logical(val)) {
        colored <- ifelse(val, "\033[32mtrue\033[0m", "\033[31mfalse\033[0m")
        cat(sprintf("%s\033[1;36m%s\033[0m: %s\n", pad, key, colored))
      } else if (is.numeric(val) && length(val) == 1) {
        cat(sprintf("%s\033[1;36m%s\033[0m: \033[33m%s\033[0m\n", pad, key, val))
      } else if (length(val) > 1) {
        cat(sprintf("%s\033[1;36m%s\033[0m: [%s]\n", pad, key,
                    paste(val, collapse = ", ")))
      } else {
        cat(sprintf("%s\033[1;36m%s\033[0m: \033[32m\"%s\"\033[0m\n", pad, key, val))
      }
    }
  } else {
    cat(sprintf("%s%s\n", pad, paste(x, collapse = ", ")))
  }
}

section <- function(title) {
  width <- 60
  bar <- strrep("-", width)
  cat(sprintf("\033[1;35m%s\033[0m\n", bar))
  cat(sprintf("\033[1;35m  %s\033[0m\n", title))
  cat(sprintf("\033[1;35m%s\033[0m\n", bar))
}

# --- Main ---

args <- commandArgs(trailingOnly = TRUE)

url <- if (length(args) >= 1) {
  args[1]
} else {
  cat("Enter API endpoint URL: ")
  readLines(con = stdin(), n = 1)
}

url <- trimws(url)
if (!grepl("^https?://", url)) {
  stop("URL must start with http:// or https://")
}

# Optional: extra headers as key=value pairs after the URL
headers <- list()
if (length(args) >= 2) {
  for (h in args[-1]) {
    parts <- strsplit(h, "=", fixed = TRUE)[[1]]
    if (length(parts) == 2) headers[[parts[1]]] <- parts[2]
  }
}

cat(sprintf("\nFetching: \033[4m%s\033[0m\n\n", url))

response <- tryCatch(
  do.call(GET, c(list(url = url, user_agent("R/api-formatter")), headers)),
  error = function(e) stop(sprintf("Request failed: %s", conditionMessage(e)))
)

# --- Response summary ---
section("RESPONSE SUMMARY")
status  <- status_code(response)
status_color <- if (status < 300) "\033[32m" else if (status < 400) "\033[33m" else "\033[31m"
cat(sprintf("  Status   : %s%d %s\033[0m\n", status_color, status, http_status(response)$message))
cat(sprintf("  URL      : %s\n", response$url))
cat(sprintf("  Time     : %.0f ms\n", response$times[["total"]] * 1000))

content_type <- headers(response)[["content-type"]]
cat(sprintf("  Content  : %s\n\n", if (!is.null(content_type)) content_type else "(none)"))

# --- Body ---
raw_text <- content(response, as = "text", encoding = "UTF-8")

if (nchar(trimws(raw_text)) == 0) {
  section("BODY")
  cat("  (empty response body)\n")
} else if (grepl("application/json", content_type %||% "", fixed = TRUE) ||
           grepl("^\\s*[\\[{]", raw_text)) {
  # JSON
  parsed <- tryCatch(
    fromJSON(raw_text, simplifyVector = FALSE),
    error = function(e) NULL
  )

  if (!is.null(parsed)) {
    section("BODY (JSON)")
    pretty_print(parsed)
    cat("\n")

    section("RAW JSON")
    cat(toJSON(parsed, pretty = TRUE, auto_unbox = TRUE), "\n")
  } else {
    section("BODY (raw text)")
    cat(raw_text, "\n")
  }
} else {
  section("BODY (raw text)")
  cat(raw_text, "\n")
}
