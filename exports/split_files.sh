#!/bin/bash

FILENAME=$1
OUTPUTPATH=$2

# benkagg_brkbasisdataselectie id field is used as key for the index in Azure Search
# It is several fields appended with . in between
# . is a non-supported character for keys in Azure Search, so replace it with _
# NOTE: Benk was asked to replace the . usage in the id field; once they do, this line can be removed
if [[ $FILENAME == "benkagg_brkbasisdataselectie" ]]; then
    echo "benkagg_brkbasisdataselectie: replacing . with '' in the id field"
    echo "This will take a while"
    jq 'if has("id") then .id |= gsub("\\."; "") else . end' $OUTPUTPATH/$FILENAME.jsonl -c > $OUTPUTPATH/temp.jsonl
    mv $OUTPUTPATH/temp.jsonl $OUTPUTPATH/$FILENAME.jsonl
fi

echo "Splitting $FILENAME into files of roughly 1000 lines each."

mkdir $OUTPUTPATH/$FILENAME


# Azure Search has a blob indexer size limit of 128 MB
# To circumvent this limit, split up the jsonlines into smaller files

# -C flag splits the file up without breaking in the middle of a line
split -l 1000 --numeric-suffixes $OUTPUTPATH/$FILENAME.jsonl $OUTPUTPATH/$FILENAME/file --additional-suffix=".jsonl"
