import logging

from triage.component.collate import (
    Aggregate,
    Categorical,
    Compare,
    SpacetimeAggregation,
    FromObj
)


def generate_spacetime_aggregations(
    feature_aggregation_config,
    as_of_dates,
    cohort_table,
    db_engine,
    features_schema_name,
    feature_start_time=None,
    materialize_subquery_fromobjs=True,
    features_ignore_cohort=False,
):
    """Creates collate.SpacetimeAggregations from the given arguments

    Args:
        feature_aggregation_config (list) all values, except for feature
            date, necessary to instantiate a collate.SpacetimeAggregation
        as_of_dates (list) dates to generate features as of
        cohort_table (string) schema.table_name for state table with all entity/date pairs
        db_engine (sqlalchemy.db.engine)
        features_schema_name (string) Name of schema where feature
            tables should be written to
        feature_start_time (string/datetime, optional) point in time before which
            should not be included in features
        materialize_subquery_fromobjs (boolean, optional) Whether or not to inspect from_obj
            values and create persistent tables out of ones that look like subqueries, for the
            purposes of making runs on many as-of-dates faster
        features_ignore_cohort (boolean, optional) Whether or not features should be built
            independently of the cohort. Takes longer but means that features can be reused
            for different cohorts.

    Returns: (list) collate.SpacetimeAggregations
    """
    if not cohort_table:
        logging.warning("No cohort table passed. Imputation will not be possible.")
        features_ignore_cohort = True

    return SpacetimeAggregationGenerator(
        db_engine=db_engine,
        features_schema_name=features_schema_name,
        feature_start_time=feature_start_time,
        materialize_subquery_fromobjs=materialize_subquery_fromobjs,
        features_ignore_cohort=features_ignore_cohort,
    ).aggregations(
        feature_aggregation_config,
        as_of_dates,
        cohort_table
    )


class SpacetimeAggregationGenerator(object):
    def __init__(
        self,
        db_engine,
        features_schema_name,
        feature_start_time=None,
        materialize_subquery_fromobjs=True,
        features_ignore_cohort=False,
    ):
        """Generates aggregate features using collate

        Args:
        db_engine (sqlalchemy.db.engine)
        features_schema_name (string) Name of schema where feature
            tables should be written to
        feature_start_time (string/datetime, optional) point in time before which
            should not be included in features
        materialize_subquery_fromobjs (boolean, optional) Whether or not to inspect from_obj
            values and create persistent tables out of ones that look like subqueries, for the
            purposes of making runs on many as-of-dates faster
        features_ignore_cohort (boolean, optional) Whether or not features should be built
            independently of the cohort. Takes longer but means that features can be reused
            for different cohorts.
        """
        self.db_engine = db_engine
        self.features_schema_name = features_schema_name
        self.categorical_cache = {}
        self.feature_start_time = feature_start_time
        self.materialize_subquery_fromobjs = materialize_subquery_fromobjs
        self.features_ignore_cohort = features_ignore_cohort
        self.entity_id_column = "entity_id"
        self.from_objs = {}

    def _compute_choices(self, choice_query):
        if choice_query not in self.categorical_cache:
            with self.db_engine.begin() as conn:
                self.categorical_cache[choice_query] = [
                    row[0] for row in conn.execute(choice_query)
                ]

            logging.info(
                "Computed list of categoricals: %s for choice query: %s",
                self.categorical_cache[choice_query],
                choice_query,
            )

        return self.categorical_cache[choice_query]

    def _build_choices(self, categorical):
        logging.info(
            "Building categorical choices for column %s, metrics %s",
            categorical["column"],
            categorical["metrics"],
        )
        if "choices" in categorical:
            logging.info("Found list of configured choices: %s", categorical["choices"])
            return categorical["choices"]
        else:
            return self._compute_choices(categorical["choice_query"])

    def _build_categoricals(self, categorical_config, impute_rules):
        # TODO: only include null flag where necessary
        return [
            Categorical(
                col=categorical["column"],
                choices=self._build_choices(categorical),
                function=categorical["metrics"],
                impute_rules=dict(
                    impute_rules,
                    coltype="categorical",
                    **categorical.get("imputation", {})
                ),
                include_null=True,
                coltype=categorical.get('coltype', None),
            )
            for categorical in categorical_config
        ]

    def _build_array_categoricals(self, categorical_config, impute_rules):
        # TODO: only include null flag where necessary
        return [
            Compare(
                col=categorical["column"],
                op="@>",
                choices={
                    choice: "array['{}'::varchar]".format(choice)
                    for choice in self._build_choices(categorical)
                },
                function=categorical["metrics"],
                impute_rules=dict(
                    impute_rules,
                    coltype="array_categorical",
                    **categorical.get("imputation", {})
                ),
                op_in_name=False,
                quote_choices=False,
                include_null=True,
                coltype=categorical.get('coltype', None)
            )
            for categorical in categorical_config
        ]

    def _aggregation(self, aggregation_config, feature_dates, state_table):
        logging.info(
            "Building collate.SpacetimeAggregation for config %s and %s as_of_dates",
            aggregation_config,
            len(feature_dates),
        )

        # read top-level imputation rules from the aggregation config; we'll allow
        # these to be overridden by imputation rules at the individual feature
        # level as those get parsed as well
        agimp = aggregation_config.get("aggregates_imputation", {})
        catimp = aggregation_config.get("categoricals_imputation", {})
        arrcatimp = aggregation_config.get("array_categoricals_imputation", {})

        aggregates = [
            Aggregate(
                aggregate["quantity"],
                aggregate["metrics"],
                dict(agimp, coltype="aggregate", **aggregate.get("imputation", {})),
                coltype=aggregate.get('coltype', None)
            )
            for aggregate in aggregation_config.get("aggregates", [])
        ]
        logging.info("Found %s quantity aggregates", len(aggregates))
        categoricals = self._build_categoricals(
            aggregation_config.get("categoricals", []), catimp
        )
        logging.info("Found %s categorical aggregates", len(categoricals))
        array_categoricals = self._build_array_categoricals(
            aggregation_config.get("array_categoricals", []), arrcatimp
        )
        logging.info("Found %s array categorical aggregates", len(array_categoricals))
        return SpacetimeAggregation(
            aggregates + categoricals + array_categoricals,
            from_obj=aggregation_config["from_obj"],
            intervals=aggregation_config["intervals"],
            groups=aggregation_config["groups"],
            as_of_dates=feature_dates,
            cohort_table=state_table,
            entity_column=self.entity_id_column,
            date_column=aggregation_config["knowledge_date_column"],
            output_date_column="as_of_date",
            db_engine=self.db_engine,
            feature_start_time=self.feature_start_time,
            features_schema_name=self.features_schema_name,
            prefix=aggregation_config["prefix"],
            features_ignore_cohort=self.features_ignore_cohort
        )

    def aggregations(self, feature_aggregation_config, feature_dates, state_table):
        return [
            self.preprocess_aggregation(
                self._aggregation(aggregation_config, feature_dates, state_table)
            )
            for aggregation_config in feature_aggregation_config
        ]

    def preprocess_aggregation(self, aggregation):
        create_schema = aggregation.get_create_schema()

        if create_schema is not None:
            with self.db_engine.begin() as conn:
                conn.execute(create_schema)

        if self.materialize_subquery_fromobjs:
            # materialize from obj
            from_obj = FromObj(
                from_obj=aggregation.from_obj.text,
                name=f"{aggregation.features_schema_name}.{aggregation.prefix}",
                knowledge_date_column=aggregation.date_column
            )
            from_obj.maybe_materialize(self.db_engine)
            aggregation.from_obj = from_obj.table
        return aggregation


FEATURE_BLOCK_GENERATOR_LOOKUP = {
    'spacetime_aggregations': generate_spacetime_aggregations
}


def feature_blocks_from_config(
    config,
    as_of_dates,
    cohort_table,
    db_engine,
    features_schema_name,
    feature_start_time=None,
    features_ignore_cohort=False,
    **kwargs
):
    """
    Create a list of feature blocks from a block of configuration
    Args:
        config (dict) feature config, consisting of:
            a key corresponding to a known feature generator (in FEATURE_BLOCK_GENERATOR_LOOKUP)
            a value corresponding to any config needed for that feature generator
        as_of_dates (list) dates to generate features as of
        cohort_table (string) schema.table_name for cohort table with all entity/date pairs
        db_engine (sqlalchemy.db.engine)
        features_schema_name (string) Name of schema where feature
            tables should be written to
        feature_start_time (string/datetime, optional) point in time before which
            should not be included in features
        features_ignore_cohort (boolean, optional) Whether or not features should be built
            independently of the cohort. Takes longer but means that features can be reused
            for different cohorts.

    Returns: (list) of FeatureBlock objects
    """
    feature_blocks = []
    for config_key, config_value in config.items():
        feature_block_generator = FEATURE_BLOCK_GENERATOR_LOOKUP.get(config_key, None)
        if not feature_block_generator:
            raise ValueError(f"feature config key {config_key} does not correspond to a recognized"
                             " feature generator.  Recognized feature generator keys:"
                             f"{FEATURE_BLOCK_GENERATOR_LOOKUP.keys()}")

        for feature_block in feature_block_generator(
            config_value,
            as_of_dates=as_of_dates,
            cohort_table=cohort_table,
            db_engine=db_engine,
            features_schema_name=features_schema_name,
            feature_start_time=feature_start_time,
            features_ignore_cohort=features_ignore_cohort,
            **kwargs
        ):
            feature_blocks.append(feature_block)
    return feature_blocks