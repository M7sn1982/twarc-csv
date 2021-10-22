import os
import json
import copy
import click
import logging
import itertools
import pandas as pd
from tqdm.auto import tqdm
from collections import ChainMap
from more_itertools import ichunked
from twarc import ensure_flattened

log = logging.getLogger("twarc")

DEFAULT_TWEET_COLUMNS = """id
conversation_id
referenced_tweets.replied_to.id
referenced_tweets.retweeted.id
referenced_tweets.quoted.id
author_id
in_reply_to_user_id
retweeted_user_id
quoted_user_id
created_at
text
lang
source
public_metrics.like_count
public_metrics.quote_count
public_metrics.reply_count
public_metrics.retweet_count
reply_settings
possibly_sensitive
withheld.scope
withheld.copyright
withheld.country_codes
entities.annotations
entities.cashtags
entities.hashtags
entities.mentions
entities.urls
context_annotations
attachments.media
attachments.media_keys
attachments.poll.duration_minutes
attachments.poll.end_datetime
attachments.poll.id
attachments.poll.options
attachments.poll.voting_status
attachments.poll_ids
author.id
author.created_at
author.username
author.name
author.description
author.entities.description.cashtags
author.entities.description.hashtags
author.entities.description.mentions
author.entities.description.urls
author.entities.url.urls
author.location
author.pinned_tweet_id
author.profile_image_url
author.protected
author.public_metrics.followers_count
author.public_metrics.following_count
author.public_metrics.listed_count
author.public_metrics.tweet_count
author.url
author.verified
author.withheld.scope
author.withheld.copyright
author.withheld.country_codes
geo.coordinates.coordinates
geo.coordinates.type
geo.country
geo.country_code
geo.full_name
geo.geo.bbox
geo.geo.type
geo.id
geo.name
geo.place_id
geo.place_type
__twarc.retrieved_at
__twarc.url
__twarc.version
""".split(
    "\n"
)

DEFAULT_USER_COLUMNS = """id
created_at
username
name
description
entities.description.cashtags
entities.description.hashtags
entities.description.mentions
entities.description.urls
entities.url.urls
location
pinned_tweet_id
pinned_tweet
profile_image_url
protected
public_metrics.followers_count
public_metrics.following_count
public_metrics.listed_count
public_metrics.tweet_count
url
verified
withheld.scope
withheld.copyright
withheld.country_codes
__twarc.retrieved_at
__twarc.url
__twarc.version
""".split(
    "\n"
)

DEFAULT_COMPLIANCE_COLUMNS = """id
action
created_at
redacted_at
reason
""".split(
    "\n"
)

DEFAULT_COUNTS_COLUMNS = """start
end
count
"""


class DataFrameConverter:
    """
    Convert a set of JSON Objects into a Pandas DataFrame object.
    You can call this directly on a small set of tweets, but memory is quickly consumed for larger datasets.

    This class can accept individual tweets or whole response objects.

    Args:
        objects (iterable): JSON Objects to convert.
        input_data_type (str): data type: `tweets` or `users` or `compliance` or `counts`
    Returns:
        DataFrame: The objects provided as a Pandas DataFrame.
    """

    def __init__(
        self,
        input_data_type="tweets",
        json_encode_all=False,
        json_encode_text=False,
        json_encode_lists=True,
        inline_referenced_tweets=False,
        merge_retweets=True,
        allow_duplicates=False,
        extra_input_columns="",
        output_columns="",
        dataset_ids=None,
        counts=None,
    ):
        self.json_encode_all = json_encode_all
        self.json_encode_text = json_encode_text
        self.json_encode_lists = json_encode_lists
        self.inline_referenced_tweets = inline_referenced_tweets
        self.merge_retweets = merge_retweets
        self.allow_duplicates = allow_duplicates

        self.columns = list()
        if "tweets" in input_data_type:
            self.columns.extend(
                x for x in DEFAULT_TWEET_COLUMNS if x not in self.columns
            )
        if "users" in input_data_type:
            self.columns.extend(
                x for x in DEFAULT_USER_COLUMNS if x not in self.columns
            )
        if "compliance" in input_data_type:
            self.columns.extend(
                x for x in DEFAULT_COMPLIANCE_COLUMNS if x not in self.columns
            )
        if "counts" in input_data_type:
            self.columns.extend(
                x for x in DEFAULT_COUNTS_COLUMNS if x not in self.columns
            )
        if extra_input_columns:
            self.columns.extend(
                x for x in extra_input_columns.split(",") if x not in self.columns
            )
        self.output_columns = (
            output_columns.split(",") if output_columns else self.columns
        )
        self.dataset_ids = dataset_ids if dataset_ids else set()
        self.counts = (
            counts
            if counts
            else {
                "lines": 0,
                "tweets": 0,
                "referenced_tweets": 0,
                "retweets": 0,
                "quotes": 0,
                "replies": 0,
                "unavailable": 0,
                "non_tweets": 0,
                "parse_errors": 0,
                "duplicates": 0,
                "rows": 0,
                "input_columns": len(self.columns),
                "output_columns": len(output_columns),
            }
        )

    def _generate_tweets(self, objects):
        """
        Generate flattened tweets from a batch of parsed lines.
        """
        for item in objects:
            for tweet in ensure_flattened(item):
                yield tweet

    def _inline_referenced_tweets(self, tweet):
        """
        (Optional) Insert referenced tweets into the main CSV as new rows
        """
        if "referenced_tweets" in tweet and self.inline_referenced_tweets:
            for referenced_tweet in tweet["referenced_tweets"]:
                # extract the referenced tweet as a new row
                self.counts["referenced_tweets"] += 1
                # inherit __twarc metadata from parent tweet
                referenced_tweet["__twarc"] = (
                    tweet["__twarc"] if "__twarc" in tweet else None
                )
                # write tweet as new row if referenced tweet exists (has more than the 3 default fields):
                if len(referenced_tweet.keys()) > 3:
                    yield self._format_tweet(referenced_tweet)
                else:
                    self.counts["unavailable"] += 1
        yield self._format_tweet(tweet)

    def _format_tweet(self, tweet):
        """
        Make the tweet objects easier to deal with, removing extra info and changing the structure
        """
        # Make a copy of the original flattened tweet
        tweet = copy.deepcopy(tweet)
        # Deal with pinned tweets for user datasets, `tweet` here is actually a user:
        # remove the tweet from a user dataset, pinned_tweet_id remains:
        tweet.pop("pinned_tweet", None)
        # Remove in_reply_to_user, in_reply_to_user_id remains:
        tweet.pop("in_reply_to_user", None)

        if "referenced_tweets" in tweet:
            # Extract Retweet only
            rts = [t for t in tweet["referenced_tweets"] if t["type"] == "retweeted"]
            retweeted_tweet = rts[-1] if rts else None
            if retweeted_tweet and "author_id" in retweeted_tweet:
                tweet["retweeted_user_id"] = retweeted_tweet["author_id"]

            # Extract Quoted tweet
            qts = [t for t in tweet["referenced_tweets"] if t["type"] == "quoted"]
            quoted_tweet = qts[-1] if qts else None
            if quoted_tweet and "author_id" in quoted_tweet:
                tweet["quoted_user_id"] = quoted_tweet["author_id"]

            # Process Retweets:
            # If it's a native retweet, replace the "RT @user Text" with the original text, metrics, and entities, but keep the Author.
            if retweeted_tweet and self.merge_retweets:
                # A retweet inherits everything from retweeted tweet.
                tweet["text"] = retweeted_tweet.pop("text", None)
                tweet["entities"] = retweeted_tweet.pop("entities", None)
                tweet["attachments"] = retweeted_tweet.pop("attachments", None)
                tweet["context_annotations"] = retweeted_tweet.pop(
                    "context_annotations", None
                )
                tweet["public_metrics"] = retweeted_tweet.pop("public_metrics", None)

            # reconstruct referenced_tweets object
            referenced_tweets = [
                {r["type"]: {"id": r["id"]}} for r in tweet["referenced_tweets"]
            ]
            # leave behind references, but not the full tweets
            # ChainMap flattens list into properties
            tweet["referenced_tweets"] = dict(ChainMap(*referenced_tweets))
        else:
            tweet["referenced_tweets"] = {}
        # Remove `type` left over from referenced tweets
        tweet.pop("type", None)
        # Remove empty objects
        if "attachments" in tweet and not tweet["attachments"]:
            tweet.pop("attachments", None)
        if "entities" in tweet and not tweet["entities"]:
            tweet.pop("entities", None)
        if "public_metrics" in tweet and not tweet["public_metrics"]:
            tweet.pop("public_metrics", None)

        return tweet

    def _process_tweets(self, tweets):
        """
        Count, deduplicate tweets before adding them to the dataframe.
        """
        for tweet in tweets:
            if "id" in tweet:
                tweet_id = tweet["id"]
                self.counts["tweets"] += 1
                if tweet_id in self.dataset_ids:
                    self.counts["duplicates"] += 1
                if self.allow_duplicates:
                    yield tweet
                else:
                    if tweet_id not in self.dataset_ids:
                        yield tweet
                self.dataset_ids.add(tweet_id)
            else:
                # non tweet objects are usually streaming API errors etc.
                self.counts["non_tweets"] += 1

    def _process_dataframe(self, _df):
        """
        Apply additional preprocessing to the DataFrame contents.
        """

        # (Optional) json encode all
        if self.json_encode_all:
            _df = _df.applymap(json.dumps, na_action="ignore")
        else:
            # (Optional) text escape for any text fields
            if self.json_encode_text:
                _df = _df.applymap(
                    lambda x: json.dumps(x) if type(x) is str else x,
                    na_action="ignore",
                )
            else:
                # Mandatory newline escape to prevent breaking csv format:
                _df = _df.applymap(
                    lambda x: x.replace("\r", "").replace("\n", r"\n")
                    if type(x) is str
                    else x,
                    na_action="ignore",
                )
            # (Optional) json for lists
            if self.json_encode_lists:
                _df = _df.applymap(
                    lambda x: json.dumps(x) if pd.api.types.is_list_like(x) else x,
                    na_action="ignore",
                )
        return _df

    def process(self, objects):
        """
        Process the objects into a pandas dataframe.
        """

        tweet_batch = itertools.chain.from_iterable(
            self._process_tweets(self._inline_referenced_tweets(tweet))
            for tweet in self._generate_tweets(objects)
        )
        _df = pd.json_normalize(list(tweet_batch), errors="ignore")

        # Check for mismatched columns
        diff = set(_df.columns) - set(self.columns)
        if len(diff) > 0:
            click.echo(
                click.style(
                    f"💔 ERROR: {len(diff)} Unexpected items in data! to fix, add these with:\n--extra-input-columns \"{','.join(diff)}\"\nSkipping entire batch of {len(_df)} tweets!",
                    fg="red",
                ),
                err=True,
            )
            log.error(
                f"CSV Unexpected Data: \"{','.join(diff)}\". Expected {len(self.columns)} columns, got {len(_df.columns)}. Skipping entire batch of {len(_df)} tweets!"
            )
            self.counts["parse_errors"] += len(_df)
            return pd.DataFrame(columns=self.columns)

        return self._process_dataframe(_df.reindex(columns=self.columns))


class CSVConverter:
    """
    JSON Reader and CSV Writer. Converts a given file into CSV, splitting it into chunks, showing progress.
    """

    def __init__(
        self,
        infile,
        outfile,
        converter=DataFrameConverter(),
        output_format="csv",
        batch_size=100,
        hide_progress=False,
    ):
        self.infile = infile
        self.outfile = outfile
        self.converter = converter
        self.output_format = output_format
        self.batch_size = batch_size
        self.hide_progress = hide_progress
        self.hide_progress = (
            infile.name == "<stdin>" or outfile.name == "<stdout>" or hide_progress
        )
        self.progress = tqdm(
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            total=os.stat(infile.name).st_size if not self.hide_progress else 1,
            disable=self.hide_progress,
        )

    def _read_lines(self):
        """
        Generator for reading files line by line from a file. Progress bar is based on file size.
        """
        line = self.infile.readline()
        while line:
            self.converter.counts["lines"] += 1
            if line.strip() != "":
                try:
                    o = json.loads(line)
                    yield o
                except Exception as ex:
                    self.converter.counts["parse_errors"] += 1
                    log.error(f"Error when trying to parse json: '{line}' {ex}")
            if not self.hide_progress:
                self.progress.update(self.infile.tell() - self.progress.n)
            line = self.infile.readline()

    def _write_output(self, _df, first_batch):
        """
        Write out the dataframe chunk by chunk

        todo: take parameters from commandline for optional output formats.
        """
        if first_batch:
            mode = "w"
            header = True
        else:
            mode = "a+"
            header = False

        self.converter.counts["rows"] += len(_df)
        _df.to_csv(
            self.outfile,
            mode=mode,
            columns=self.converter.output_columns,
            index=False,
            header=header,
        )  # todo: (Optional) arguments for to_csv

    def process(self):
        """
        Process a file containing JSON into a CSV
        """

        # Flag for writing header & appending to CSV file
        first_batch = True
        for batch in ichunked(self._read_lines(), self.batch_size):
            self._write_output(self.converter.process(batch), first_batch)
            first_batch = False

        self.progress.close()


@click.command()
@click.argument("infile", type=click.File("r", encoding="utf8"), default="-")
@click.argument("outfile", type=click.File("w", encoding="utf8"), default="-")
@click.option(
    "--input-data-type",
    required=False,
    default="tweets",
    help='Input data type - you can turn "tweets", "users", "counts" or "compliance" data into CSV.',
    type=click.Choice(
        ["tweets", "users", "counts", "compliance"], case_sensitive=False
    ),
)
@click.option(
    "--json-encode-all/--no-json-encode-all",
    default=False,
    help="JSON encode / escape all fields. Default: no",
)
@click.option(
    "--json-encode-text/--no-json-encode-text",
    default=False,
    help="Apply JSON encode / escape to text fields. Default: no",
)
@click.option(
    "--inline-referenced-tweets/--no-inline-referenced-tweets",
    default=False,
    help="Output referenced tweets inline as separate rows. Default: no.",
)
@click.option(
    "--json-encode-lists/--no-json-encode-lists",
    default=True,
    help="JSON encode / escape lists. Default: yes",
)
@click.option(
    "--merge-retweets/--no-merge-retweets",
    default=True,
    help="Merge original tweet metadata into retweets. The Retweet Text, metrics and entities are merged from the original tweet. Default: Yes.",
)
@click.option(
    "--allow-duplicates",
    is_flag=True,
    default=False,
    help="List every tweets as is, including duplicates. Default: No, only unique tweets per row. Retweets are not duplicates.",
)
@click.option(
    "--extra-input-columns",
    default="",
    help="Manually specify extra input columns. Comma separated string. Only modify this if you have processed the json yourself. Default output is all available object columns, no extra input columns.",
)
@click.option(
    "--output-columns",
    default="",
    help="Specify what columns to output in the CSV. Default is all input columns.",
)
@click.option(
    "--batch-size",
    type=int,
    default=100,
    help="How many lines to process per chunk. Default is 100. Reduce this if output is slow.",
)
@click.option(
    "--hide-stats",
    is_flag=True,
    default=False,
    help="Hide stats about the dataset on completion. Always hidden if you're using stdin / stdout pipes.",
)
@click.option(
    "--hide-progress",
    is_flag=True,
    default=False,
    help="Hide the Progress bar. Always hidden if you're using stdin / stdout pipes.",
)
def csv(
    infile,
    outfile,
    input_data_type,
    json_encode_all,
    json_encode_text,
    json_encode_lists,
    inline_referenced_tweets,
    merge_retweets,
    allow_duplicates,
    extra_input_columns,
    output_columns,
    batch_size,
    hide_stats,
    hide_progress,
):
    """
    Convert tweets to CSV.
    """

    if infile.name == outfile.name:
        click.echo(
            click.style(
                f"💔 Cannot convert files in-place, specify a different output file!",
                fg="red",
            ),
            err=True,
        )
        return

    if (
        not (infile.name == "<stdin>" or outfile.name == "<stdout>")
        and os.stat(infile.name).st_size == 0
    ):
        click.echo(
            click.style(
                f"💔 Input file is empty! Nothing to convert.",
                fg="red",
            ),
            err=True,
        )
        return

    converter = DataFrameConverter(
        input_data_type=input_data_type,
        json_encode_all=json_encode_all,
        json_encode_text=json_encode_text,
        json_encode_lists=json_encode_lists,
        inline_referenced_tweets=inline_referenced_tweets,
        merge_retweets=merge_retweets,
        allow_duplicates=allow_duplicates,
        extra_input_columns=extra_input_columns,
        output_columns=output_columns,
    )

    writer = CSVConverter(
        infile=infile,
        outfile=outfile,
        converter=converter,
        output_format="csv",
        batch_size=batch_size,
        hide_progress=hide_progress,
    )
    writer.process()

    if not hide_stats and outfile.name != "<stdout>":

        errors = (
            click.style(
                f"{converter.counts['parse_errors']} failed to parse. See twarc.log for details.\n",
                fg="red",
            )
            if converter.counts["parse_errors"] > 0
            else ""
        )

        referenced_stats = (
            f"{converter.counts['referenced_tweets']} were referenced tweets, {converter.counts['duplicates']} were referenced multiple times, and {converter.counts['unavailable']} were referenced but not available in the API responses.\n"
            if inline_referenced_tweets
            else ""
        )

        click.echo(
            f"\nℹ️\n"
            + f"Parsed {converter.counts['tweets']} tweets from {converter.counts['lines']} lines in the file, and {converter.counts['non_tweets']} non tweet objects.\n"
            + referenced_stats
            + errors
            + f"Wrote {converter.counts['rows']} rows and output {converter.counts['output_columns']} of {converter.counts['input_columns']} input columns in the CSV.\n",
            err=True,
        )
